Audit Report

## Title
Trivial-PoW CompactBlock Flooding via Missing `compact_target` Consensus Check in `HeaderVerifier` — (`sync/src/relayer/compact_block_process.rs`, `verification/src/header_verifier.rs`)

## Summary

The CompactBlock relay path explicitly bypasses rate limiting under the assumption that PoW imposes a real cost barrier. However, the `HeaderVerifier` invoked during `contextual_check` does not validate that the header's `compact_target` matches the consensus-expected epoch target. With `compact_target=0x20ffffff` (yielding `block_target = U256::max_value()`), any Eaglesong output trivially satisfies the PoW check, making the cost barrier nonexistent. An attacker can flood the `HeaderMap` and sled backend with zero real mining cost, exhausting memory and disk resources and crashing the node.

## Finding Description

**Step 1 — Rate limiting is unconditionally bypassed for CompactBlock:** [1](#0-0) 

All CompactBlock messages skip the rate limiter with the stated justification that PoW provides the cost barrier.

**Step 2 — `non_contextual_check` performs no PoW or `compact_target` check:** [2](#0-1) 

Only uncle count, proposals count, and block height staleness are checked. No `compact_target` validation occurs.

**Step 3 — `contextual_check` calls `HeaderVerifier::verify`, which calls `PowVerifier`:** [3](#0-2) [4](#0-3) 

`PowVerifier` in `pow/src/eaglesong.rs` computes `compact_to_target(header.raw().compact_target())` and checks `output <= block_target`: [5](#0-4) 

With `compact_target=0x20ffffff`, `compact_to_target` returns `(U256::max_value(), false)`. Since `U256::max_value()` is the maximum possible value, **any** Eaglesong output satisfies `output <= block_target`. Any nonce works — zero real mining cost.

**Step 4 — `EpochVerifier` in `header_verifier.rs` does NOT check `compact_target` against consensus:** [6](#0-5) 

The `EpochVerifier` used in the header-only path only checks epoch format (`is_well_formed`) and epoch succession (`is_successor_of`). It never validates that the header's `compact_target` matches the consensus-expected target for that epoch.

**Step 5 — The `compact_target` vs. consensus epoch target check only exists in `contextual_block_verifier.rs`:** [7](#0-6) 

This check runs during **full block** verification — after `HeaderMap` state has already been mutated.

**Step 6 — `CompactBlockVerifier::verify` does not check `compact_target`:** [8](#0-7) 

It only validates prefilled transaction structure and short ID uniqueness.

**Step 7 — `insert_valid_header` mutates `HeaderMap` state before full block verification:** [9](#0-8) [10](#0-9) 

After `contextual_check` passes (which it does with `compact_target=0x20ffffff`), the header is unconditionally inserted into `HeaderMap` with no `compact_target` validation.

**Step 8 — `HeaderMap` overflows to an unbounded sled backend:** [11](#0-10) 

The in-memory limit is 256 MB (default). The `limit_memory` task runs every 5 seconds and spills excess entries to a sled on-disk backend. The sled backend has no size cap: [12](#0-11) 

**Step 9 — Attacker can chain headers indefinitely:**

After block N is inserted into `HeaderMap`, `get_header_index_view` finds it as the parent for block N+1: [13](#0-12) 

This allows the attacker to build an unbounded chain of trivially-mined headers, each passing all checks and being inserted into `HeaderMap`.

**Step 10 — No `min_chain_work` guard in the relayer path:**

A search of `sync/src/relayer/compact_block_process.rs` confirms no `min_chain_work` check exists in this code path. The `min_chain_work` checks found in `sync/src/types/mod.rs` and `sync/src/synchronizer/mod.rs` apply to the synchronizer path, not the relayer's compact block processing path.

## Impact Explanation

This vulnerability enables a remote, unprivileged attacker to exhaust both memory (up to 256 MB of `HeaderMap` in-process memory) and disk (unbounded sled backend growth) on a target CKB node. Continuous flooding causes disk space exhaustion, I/O starvation, and ultimately node crash. There is no cleanup of `HeaderMap` entries when `accept_block` later rejects the block at `ContextualBlockVerifier::EpochVerifier`. This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

- Requires only a standard P2P connection — no privileges, no keys, no majority hashpower.
- `compact_target=0x20ffffff` requires zero real mining work (any nonce satisfies the PoW check).
- The rate limiter is explicitly disabled for CompactBlock messages.
- The attack is trivially scriptable: build a chain of headers with valid epoch succession but trivial `compact_target`, wrap each in a CompactBlock, and send over P2P.
- The attack is repeatable and continuous with no per-message cost to the attacker.

## Recommendation

1. **Add `compact_target` validation to `HeaderVerifier`**: The `EpochVerifier` in `verification/src/header_verifier.rs` should verify that the header's `compact_target` matches the expected target derived from the parent's epoch, mirroring the check already present in `contextual_block_verifier.rs`.
2. **Re-enable rate limiting for CompactBlock**: The justification "CompactBlock will be verified by POW" is only valid when PoW imposes a real cost. Since `compact_target` is attacker-controlled and not validated against consensus before the rate-limit bypass, the assumption is broken. Apply a per-peer rate limit to CompactBlock messages.
3. **Add a `compact_target` sanity check in `non_contextual_check`**: Reject headers whose `compact_target` deviates beyond a reasonable bound from the current epoch's target, or is below the minimum difficulty.

## Proof of Concept

```python
# Pseudocode
parent = get_chain_tip()  # real chain tip
for i in range(N):
    header = build_header(
        parent_hash=parent.hash,
        number=parent.number + 1,
        epoch=successor_epoch(parent.epoch),  # valid epoch succession
        compact_target=0x20ffffff,             # trivially easy, any nonce works
        timestamp=parent.timestamp + 1000,
        nonce=0,                               # any nonce satisfies PoW check
    )
    compact_block = build_compact_block(header, prefilled_txs=[cellbase])
    send_p2p(compact_block)  # no rate limit applied
    parent = header
    # Each iteration: HeaderMap grows by one entry
    # After 256MB in-memory limit: entries spill to sled backend (no disk cap)
    # compact_target mismatch only caught in ContextualBlockVerifier during
    # full block processing — after HeaderMap is already mutated
```

After N iterations, `HeaderMap` memory is exhausted and the sled backend grows proportionally until disk is full, crashing the node.

### Citations

**File:** sync/src/relayer/mod.rs (L112-114)
```rust
        // CompactBlock will be verified by POW, it's OK to skip rate limit checking.
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));
```

**File:** sync/src/relayer/compact_block_process.rs (L77-78)
```rust
        // Header has been verified ok, update state
        shared.insert_valid_header(self.peer, &header);
```

**File:** sync/src/relayer/compact_block_process.rs (L190-223)
```rust
fn non_contextual_check(
    compact_block: &CompactBlock,
    header: &HeaderView,
    consensus: &Consensus,
    active_chain: &ActiveChain,
) -> Status {
    if compact_block.uncles().len() > consensus.max_uncles_num() {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "CompactBlock uncles count({}) > consensus max_uncles_num({})",
            compact_block.uncles().len(),
            consensus.max_uncles_num()
        ));
    }
    if (compact_block.proposals().len() as u64) > consensus.max_block_proposals_limit() {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "CompactBlock proposals count({}) > consensus max_block_proposals_limit({})",
            compact_block.proposals().len(),
            consensus.max_block_proposals_limit(),
        ));
    }

    // Only accept blocks with a height greater than tip - N
    // where N is the current epoch length
    let block_hash = header.hash();
    let tip = active_chain.tip_header();
    let epoch_length = active_chain.epoch_ext().length();
    let lowest_number = tip.number().saturating_sub(epoch_length);

    if lowest_number > header.number() {
        return StatusCode::CompactBlockIsStaled.with_context(block_hash);
    }

    Status::ok()
}
```

**File:** sync/src/relayer/compact_block_process.rs (L263-267)
```rust
    let store_first = tip.number() + 1 >= compact_block_header.number();
    let parent = shared.get_header_index_view(
        &compact_block_header.data().raw().parent_hash(),
        store_first,
    );
```

**File:** sync/src/relayer/compact_block_process.rs (L324-325)
```rust
    let header_verifier = HeaderVerifier::new(&median_time_context, shared.consensus());
    if let Err(err) = header_verifier.verify(compact_block_header) {
```

**File:** verification/src/header_verifier.rs (L32-34)
```rust
    fn verify(&self, header: &Self::Target) -> Result<(), Error> {
        // POW check first
        PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
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

**File:** pow/src/eaglesong.rs (L16-27)
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
            if log_enabled!(Debug) {
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L500-507)
```rust
        let actual_compact_target = header.compact_target();
        if self.epoch.compact_target() != actual_compact_target {
            return Err(EpochError::TargetMismatch {
                expected: self.epoch.compact_target(),
                actual: actual_compact_target,
            }
            .into());
        }
```

**File:** sync/src/relayer/compact_block_verifier.rs (L11-15)
```rust
    pub(crate) fn verify(block: &packed::CompactBlock) -> Status {
        attempt!(PrefilledVerifier::verify(block));
        attempt!(ShortIdsVerifier::verify(block));
        Status::ok()
    }
```

**File:** sync/src/types/mod.rs (L1129-1129)
```rust
        self.shared.header_map().insert(header_view.clone());
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
