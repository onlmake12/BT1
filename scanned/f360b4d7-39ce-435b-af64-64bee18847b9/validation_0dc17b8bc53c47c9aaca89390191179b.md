The critical finding is now clear. There are **two different `EpochVerifier` implementations** — one used in the compact block relay path that omits the `compact_target` check, and one used in full block contextual verification that does check it. This is the root cause.

---

### Title
Missing `compact_target` Epoch Difficulty Validation in Compact Block Header Relay Path Enables Zero-Cost HeaderMap Flooding — (`sync/src/relayer/compact_block_process.rs`, `verification/src/header_verifier.rs`)

---

### Summary

An unprivileged remote peer can relay an unlimited number of `CompactBlock` messages whose headers carry `compact_target = 0x20ffffff` (maximum target, minimum difficulty). Because the relay path's `HeaderVerifier` omits the epoch-difficulty check that the contextual block verifier enforces, the PoW check passes trivially for any nonce, and every such header is inserted into the `HeaderMap` and `pending_compact_blocks` state with zero real computational cost. Rate limiting is explicitly disabled for `CompactBlock` messages.

---

### Finding Description

**Rate limiting is explicitly skipped for `CompactBlock`:** [1](#0-0) 

The comment reads: *"CompactBlock will be verified by POW, it's OK to skip rate limit checking."* This assumption is the root of the problem.

**`EaglesongPowEngine::verify` with `compact_target = 0x20ffffff`:**

`compact_to_target(0x20ffffff)` yields `(U256::max_value(), false)` — confirmed by the test suite: [2](#0-1) 

With `block_target = U256::max_value()`, the check `eaglesong_output > block_target` is always `false`, so `verify()` returns `true` for any nonce: [3](#0-2) 

**The relay path's `HeaderVerifier` does NOT check `compact_target` against the expected epoch difficulty:**

`contextual_check` calls `HeaderVerifier::verify`, which runs only four sub-verifiers: [4](#0-3) 

The `EpochVerifier` used here only checks epoch continuity (`is_well_formed` and `is_successor_of`), never the `compact_target` value: [5](#0-4) 

**The REAL epoch-difficulty check exists only in the contextual block verifier**, which runs after state mutation: [6](#0-5) 

This verifier checks `self.epoch.compact_target() != actual_compact_target` and returns `EpochError::TargetMismatch`, but it is invoked only during full block acceptance — after `insert_valid_header` has already mutated the `HeaderMap`.

**State mutation happens unconditionally after the relay-path header check passes:** [7](#0-6) 

`insert_valid_header` inserts into the `HeaderMap` and updates `shared_best_header`: [8](#0-7) 

**`HeaderMap` memory is bounded at 256 MB by default, but the sled backend is unbounded on disk:** [9](#0-8) 

When memory overflows, entries are spilled to the sled backend every 5 seconds: [10](#0-9) 

The sled backend has no size cap: [11](#0-10) 

**`pending_compact_blocks` is also polluted** when block reconstruction fails (missing transactions), inserting the attacker's compact block into the unbounded `HashMap`: [12](#0-11) 

---

### Impact Explanation

- **Memory exhaustion**: Up to 256 MB of `HeaderMap` memory consumed by zero-cost headers.
- **Unbounded disk I/O**: Sled backend grows without limit as the 5-second `limit_memory` task spills entries to disk.
- **`pending_compact_blocks` pollution**: Each block whose reconstruction fails (all of them, since the attacker's chain has no real transactions) is inserted into `pending_compact_blocks`, holding the compact block in memory indefinitely.
- **CPU overhead**: Each message triggers `HeaderVerifier::verify` (PoW hash computation, median-time lookup) and `insert_valid_header` (skip-list construction).
- **Network amplification**: For each compact block with missing transactions, the node sends a `GetBlockTransactions` request back to the attacker.

---

### Likelihood Explanation

The attack requires no privileged access, no hashpower, and no special network position. Any peer that can establish a P2P connection can execute it. Building a chain of headers with `compact_target = 0x20ffffff`, valid epoch succession, and monotonically increasing timestamps is trivial — any nonce satisfies PoW. The attacker can generate and relay thousands of such headers per second.

---

### Recommendation

Add a `compact_target` validation step to the relay-path `HeaderVerifier` (or to `contextual_check` directly) that computes the expected epoch target from the parent's epoch extension and rejects headers whose `compact_target` does not match. This mirrors the check already present in `verification/contextual/src/contextual_block_verifier.rs` `EpochVerifier::verify` at lines 500–507. The fix must run **before** `insert_valid_header` is called.

---

### Proof of Concept

1. Connect to a mainnet/testnet node as a peer.
2. Build a chain starting from genesis: for each block N, set `compact_target = 0x20ffffff`, `number = N`, `epoch = valid_successor(parent.epoch)`, `timestamp = parent.timestamp + 1`, `parent_hash = hash(block_{N-1})`, any `nonce`.
3. Wrap each block as a `CompactBlock` (prefill only the cellbase).
4. Send each `CompactBlock` over the relay protocol. No rate limit applies.
5. Observe: `HeaderMap` memory grows proportionally; sled backend receives writes every 5 seconds; `pending_compact_blocks` accumulates entries; `GetBlockTransactions` messages are sent back to the attacker.
6. The epoch-difficulty mismatch is only caught when the full block is submitted for contextual verification — which never happens in this attack since the attacker never sends the full block.

### Citations

**File:** sync/src/relayer/mod.rs (L112-114)
```rust
        // CompactBlock will be verified by POW, it's OK to skip rate limit checking.
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));
```

**File:** util/types/src/utilities/tests/difficulty.rs (L25-34)
```rust
        let compact_when_target_is_max = 0x20ffffff;

        let compact = target_to_compact(U256::max_value());
        assert_eq!(compact, compact_when_target_is_max);

        let difficulty = compact_to_difficulty(compact);
        assert_eq!(difficulty, U256::one());

        let compact_from_difficulty = difficulty_to_compact(difficulty);
        assert_eq!(compact, compact_from_difficulty);
```

**File:** pow/src/eaglesong.rs (L16-26)
```rust
        let (block_target, overflow) = compact_to_target(header.raw().compact_target().into());

        if block_target.is_zero() || overflow {
            debug!(
                "compact_target is invalid: {:#x}",
                header.raw().compact_target()
            );
            return false;
        }

        if U256::from_big_endian(&output[..]).expect("bound checked") > block_target {
```

**File:** verification/src/header_verifier.rs (L32-50)
```rust
    fn verify(&self, header: &Self::Target) -> Result<(), Error> {
        // POW check first
        PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
        let parent_fields = self
            .data_loader
            .get_header_fields(&header.parent_hash())
            .ok_or_else(|| UnknownParentError {
                parent_hash: header.parent_hash(),
            })?;
        NumberVerifier::new(parent_fields.number, header).verify()?;
        EpochVerifier::new(parent_fields.epoch, header).verify()?;
        TimestampVerifier::new(
            self.data_loader,
            header,
            self.consensus.median_time_block_count(),
        )
        .verify()?;
        Ok(())
    }
```

**File:** verification/src/header_verifier.rs (L133-148)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        if !self.header.epoch().is_well_formed() {
            return Err(EpochError::Malformed {
                value: self.header.epoch(),
            }
            .into());
        }
        if !self.parent.is_genesis() && !self.header.epoch().is_successor_of(self.parent) {
            return Err(EpochError::NonContinuous {
                current: self.header.epoch(),
                parent: self.parent,
            }
            .into());
        }
        Ok(())
    }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L488-509)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let header = self.block.header();
        let actual_epoch_with_fraction = header.epoch();
        let block_number = header.number();
        let epoch_with_fraction = self.epoch.number_with_fraction(block_number);
        if actual_epoch_with_fraction != epoch_with_fraction {
            return Err(EpochError::NumberMismatch {
                expected: epoch_with_fraction.full_value(),
                actual: actual_epoch_with_fraction.full_value(),
            }
            .into());
        }
        let actual_compact_target = header.compact_target();
        if self.epoch.compact_target() != actual_compact_target {
            return Err(EpochError::TargetMismatch {
                expected: self.epoch.compact_target(),
                actual: actual_compact_target,
            }
            .into());
        }
        Ok(())
    }
```

**File:** sync/src/relayer/compact_block_process.rs (L75-78)
```rust
        // The new arrived has greater difficulty than local best known chain
        attempt!(CompactBlockVerifier::verify(&compact_block));
        // Header has been verified ok, update state
        shared.insert_valid_header(self.peer, &header);
```

**File:** sync/src/types/mod.rs (L979-987)
```rust
// <CompactBlockHash, (CompactBlock, <PeerIndex, (Vec<TransactionsIndex>, Vec<UnclesIndex>)>, timestamp)>
pub(crate) type PendingCompactBlockMap = HashMap<
    Byte32,
    (
        packed::CompactBlock,
        HashMap<PeerIndex, (Vec<u32>, Vec<u32>)>,
        u64,
    ),
>;
```

**File:** sync/src/types/mod.rs (L1094-1141)
```rust
    pub fn insert_valid_header(&self, peer: PeerIndex, header: &core::HeaderView) {
        let tip_number = self.active_chain().tip_number();
        let store_first = tip_number >= header.number();
        // We don't use header#parent_hash clone here because it will hold the arc counter of the SendHeaders message
        // which will cause the 2000 headers to be held in memory for a long time
        let parent_hash = Byte32::from_slice(header.data().raw().parent_hash().as_slice())
            .expect("checked slice length");
        let parent_header_index = self
            .get_header_index_view(&parent_hash, store_first)
            .expect("parent should be verified");
        let mut header_view = HeaderIndexView::new(
            header.hash(),
            header.number(),
            header.epoch(),
            header.timestamp(),
            parent_hash,
            parent_header_index.total_difficulty() + header.difficulty(),
        );

        let snapshot = Arc::clone(&self.shared.snapshot());
        header_view.build_skip(
            tip_number,
            |hash, store_first| self.get_header_index_view(hash, store_first),
            |number, current| {
                // shortcut to return an ancestor block
                if current.number <= snapshot.tip_number() && snapshot.is_main_chain(&current.hash)
                {
                    snapshot
                        .get_block_hash(number)
                        .and_then(|hash| self.get_header_index_view(&hash, true))
                } else {
                    None
                }
            },
        );
        self.shared.header_map().insert(header_view.clone());
        self.state
            .peers()
            .may_set_best_known_header(peer, header_view.as_header_index());
        if header_view.number().is_multiple_of(10000) {
            info!(
                "inserted valid header: header {}-{}",
                header_view.number(),
                header_view.hash()
            );
        }
        self.state.may_set_shared_best_header(header_view);
    }
```

**File:** util/app-config/src/configs/network.rs (L214-216)
```rust
const fn default_memory_limit() -> ByteUnit {
    ByteUnit::Megabyte(256)
}
```

**File:** shared/src/types/header_map/mod.rs (L29-76)
```rust
const INTERVAL: Duration = Duration::from_millis(5000);
const ITEM_BYTES_SIZE: usize = size_of::<HeaderIndexView>();
const WARN_THRESHOLD: usize = ITEM_BYTES_SIZE * 100_000;

impl HeaderMap {
    pub fn new<P>(
        tmpdir: Option<P>,
        memory_limit: usize,
        async_handle: &Handle,
        ibd_finished: Arc<AtomicBool>,
    ) -> Self
    where
        P: AsRef<path::Path>,
    {
        if memory_limit < ITEM_BYTES_SIZE {
            panic!("The limit setting is too low");
        }
        if memory_limit < WARN_THRESHOLD {
            ckb_logger::warn!(
                "The low memory limit setting {} will result in inefficient synchronization",
                memory_limit
            );
        }
        let size_limit = memory_limit / ITEM_BYTES_SIZE;
        let inner = Arc::new(HeaderMapKernel::new(tmpdir, size_limit, ibd_finished));
        let map_weak = Arc::downgrade(&inner);
        let stop_rx: CancellationToken = new_tokio_exit_rx();

        async_handle.spawn(async move {
            let mut interval = tokio::time::interval(INTERVAL);
            interval.set_missed_tick_behavior(MissedTickBehavior::Skip);
            loop {
                tokio::select! {
                    _ = interval.tick() => {
                        if let Some(map) = map_weak.upgrade() {
                            map.limit_memory();
                        } else {
                            debug!("HeaderMap inner was dropped, exiting background task");
                            break;
                        }
                    }
                    _ = stop_rx.cancelled() => {
                        info!("HeaderMap limit_memory received exit signal, exit now");
                        break
                    },
                }
            }
        });
```

**File:** shared/src/types/header_map/backend_sled.rs (L63-73)
```rust
    fn insert(&self, value: &HeaderIndexView) -> Option<()> {
        let key = value.hash();
        let last_value = self
            .db
            .insert(key.as_slice(), value.to_vec())
            .expect("failed to insert item to sled");
        if last_value.is_none() {
            self.count.fetch_add(1, Ordering::SeqCst);
        }
        last_value.map(|_| ())
    }
```
