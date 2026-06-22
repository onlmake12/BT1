Let me check the `min_chain_work` guard and the `EpochVerifier` in `header_verifier.rs` more carefully to confirm whether any guard blocks this path.

Based on my thorough code trace, this is a **real vulnerability**. Here is the complete analysis:

---

### Title
Trivial-PoW CompactBlock Flooding via Missing `compact_target` Consensus Check in `HeaderVerifier` — (`sync/src/relayer/compact_block_process.rs`, `verification/src/header_verifier.rs`)

### Summary

The CompactBlock relay path explicitly skips rate limiting with the stated justification that "CompactBlock will be verified by POW." However, the `HeaderVerifier` invoked in `contextual_check` does **not** verify that the header's `compact_target` matches the consensus-expected epoch target. With `compact_target=0x20ffffff` (yielding `block_target = U256::max_value()`), any Eaglesong output trivially satisfies the PoW check, making the cost barrier nonexistent. An attacker can flood the `HeaderMap` and sled backend with zero real mining cost.

### Finding Description

**Step 1 — Rate limiting is explicitly bypassed for CompactBlock:**

In `sync/src/relayer/mod.rs`, the rate limiter is unconditionally skipped for all `CompactBlock` messages:

```rust
// CompactBlock will be verified by POW, it's OK to skip rate limit checking.
let should_check_rate =
    !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));
``` [1](#0-0) 

**Step 2 — `non_contextual_check` performs no PoW check:**

`non_contextual_check` only validates uncle count, proposals count, and block height staleness. No PoW or `compact_target` check occurs here. [2](#0-1) 

**Step 3 — `contextual_check` calls `HeaderVerifier::verify`, which calls `PowVerifier`:**

`PowVerifier::verify` in `pow/src/eaglesong.rs` checks:
```rust
let (block_target, overflow) = compact_to_target(header.raw().compact_target().into());
if block_target.is_zero() || overflow { return false; }
if U256::from_big_endian(&output[..]).expect("bound checked") > block_target {
    return false;
}
true
``` [3](#0-2) 

With `compact_target=0x20ffffff`, `compact_to_target` returns `(U256::max_value(), false)` — confirmed by the test:
```rust
let compact_when_target_is_max = 0x20ffffff;
let compact = target_to_compact(U256::max_value());
assert_eq!(compact, compact_when_target_is_max);
``` [4](#0-3) 

Since `U256::max_value()` is the maximum possible value, **any** Eaglesong output satisfies `output <= block_target`. Any nonce works.

**Step 4 — `EpochVerifier` in `HeaderVerifier` does NOT check `compact_target` against consensus:**

The `EpochVerifier` used in the header verification path only checks epoch format and continuity with the parent — it never validates that the header's `compact_target` matches the consensus-expected target for that epoch:

```rust
fn verify(&self) -> Result<(), Error> {
    if !self.header.epoch().is_well_formed() { ... }
    if !self.parent.is_genesis() && !self.header.epoch().is_successor_of(self.parent) { ... }
    Ok(())
}
``` [5](#0-4) 

The `compact_target` vs. consensus epoch target check only exists in `contextual_block_verifier.rs`'s `EpochVerifier`, which runs during **full block** verification — after state has already been mutated: [6](#0-5) 

**Step 5 — `insert_valid_header` mutates `HeaderMap` state before full block verification:**

After `contextual_check` passes, `insert_valid_header` is called unconditionally:
```rust
// Header has been verified ok, update state
shared.insert_valid_header(self.peer, &header);
``` [7](#0-6) 

`insert_valid_header` inserts into `HeaderMap` with no `compact_target` validation: [8](#0-7) 

**Step 6 — `HeaderMap` overflows to unbounded sled backend:**

The in-memory limit is 256 MB (default), but overflow is spilled to a sled on-disk backend with no size cap. The `limit_memory` task runs every 5 seconds and moves excess entries to sled: [9](#0-8) [10](#0-9) 

**Step 7 — Attacker can chain headers indefinitely:**

After block N is inserted into `HeaderMap`, `get_header_index_view` will find it as the parent for block N+1, allowing the attacker to build an unbounded chain of trivially-mined headers: [11](#0-10) 

### Impact Explanation

- **HeaderMap memory exhaustion**: Up to 256 MB of in-process memory consumed by attacker-controlled headers.
- **Sled backend disk exhaustion**: Once the memory limit is hit, entries spill to the sled backend with no disk cap. Continuous flooding exhausts disk space, causing node crashes or I/O starvation.
- **Processing overhead**: Each CompactBlock triggers `HeaderVerifier`, `insert_valid_header`, `build_skip`, and sled I/O — all with zero real PoW cost to the attacker.
- **No cleanup on block rejection**: When `accept_block` later rejects the block (at `EpochVerifier` in `ContextualBlockVerifier` which does check `compact_target`), the `HeaderMap` entry is not removed.

### Likelihood Explanation

- Requires only a standard P2P connection — no privileges, no keys, no majority hashpower.
- Mining with `compact_target=0x20ffffff` requires zero real work (any nonce satisfies the check).
- The rate limiter is explicitly disabled for this message type.
- The attack is trivially scriptable and locally testable.

### Recommendation

1. **Add `compact_target` validation to `HeaderVerifier`**: The `EpochVerifier` in `verification/src/header_verifier.rs` should also verify that the header's `compact_target` matches the expected target derived from the parent's epoch. This is the check already present in `contextual_block_verifier.rs` but missing from the header-only path.
2. **Re-enable rate limiting for CompactBlock**: The justification "CompactBlock will be verified by POW" is only valid when PoW imposes a real cost. Since the `compact_target` is attacker-controlled and not validated against consensus before rate-limit bypass, the assumption is broken. Apply a per-peer rate limit to CompactBlock messages.
3. **Add a `compact_target` sanity check in `non_contextual_check`**: Reject headers whose `compact_target` is below the minimum difficulty (`DIFF_TWO = 0x2080_0000`) or deviates beyond a reasonable bound from the current epoch's target.

### Proof of Concept

```python
# Pseudocode
parent = get_chain_tip()  # real chain tip
for i in range(N):
    header = build_header(
        parent_hash=parent.hash,
        number=parent.number + 1,
        epoch=successor_epoch(parent.epoch),  # valid epoch succession
        compact_target=0x20ffffff,             # trivially easy
        timestamp=parent.timestamp + 1000,
        nonce=0,                               # any nonce works
    )
    compact_block = build_compact_block(header, transactions=[cellbase])
    send_p2p(compact_block)  # no rate limit applied
    parent = header
    # Each iteration: HeaderMap grows by one entry, sled backend written after 256MB
```

After N iterations, `HeaderMap` memory is exhausted and sled backend grows proportionally. The epoch mismatch (`compact_target=0x20ffffff` vs. consensus target) is only caught in `ContextualBlockVerifier::EpochVerifier` during full block processing — after `HeaderMap` state has already been mutated.

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

**File:** sync/src/relayer/compact_block_process.rs (L263-280)
```rust
    let store_first = tip.number() + 1 >= compact_block_header.number();
    let parent = shared.get_header_index_view(
        &compact_block_header.data().raw().parent_hash(),
        store_first,
    );
    if parent.is_none() {
        debug_target!(
            crate::LOG_TARGET_RELAY,
            "UnknownParent: {}, send_getheaders_to_peer({})",
            block_hash,
            peer
        );
        active_chain.send_getheaders_to_peer(nc, peer, (&tip).into());
        return StatusCode::CompactBlockRequiresParent.with_context(format!(
            "{} parent: {}",
            block_hash,
            compact_block_header.data().raw().parent_hash(),
        ));
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

**File:** util/app-config/src/configs/network.rs (L214-216)
```rust
const fn default_memory_limit() -> ByteUnit {
    ByteUnit::Megabyte(256)
}
```
