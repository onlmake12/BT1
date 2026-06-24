I have verified every step of the claim against the actual code. Here is my assessment:

**Step 1 (rate limit bypass):** Confirmed at [1](#0-0)  — CompactBlock unconditionally skips the rate limiter.

**Step 2 (non_contextual_check):** Confirmed at [2](#0-1)  — only uncle count, proposals count, and block height staleness are checked; no PoW or `compact_target` validation.

**Step 3 (PowVerifier with trivial target):** Confirmed at [3](#0-2)  — `compact_to_target(0x20ffffff)` returns `(U256::max_value(), false)`, so any Eaglesong output satisfies `output <= block_target`. Confirmed by test at [4](#0-3) .

**Step 4 (EpochVerifier in header_verifier.rs has no compact_target check):** Confirmed at [5](#0-4)  — only `is_well_formed()` and `is_successor_of()` are checked; no `compact_target` vs. consensus target comparison.

**Step 5 (compact_target check only in contextual_block_verifier):** Confirmed at [6](#0-5)  — this check runs only during full block verification, after `insert_valid_header` has already mutated state.

**Step 6 (insert_valid_header before full verification):** Confirmed at [7](#0-6)  and [8](#0-7) .

**Step 7 (sled backend overflow):** Confirmed at [9](#0-8)  — 5-second `limit_memory` task spills to sled with no disk cap.

**Step 8 (chaining):** Confirmed — `contextual_check` calls `shared.get_header_index_view` for the parent at [10](#0-9) , which will find previously inserted fake headers in `HeaderMap`, enabling unbounded chaining.

**No blocking guards found:** The `min_chain_work` guard is in the block download scheduler (`can_start` in `sync/src/synchronizer/mod.rs`), not in the CompactBlock relay path. The IBD check at `sync/src/relayer/mod.rs` line 816 only blocks processing during IBD — the attack applies to synced nodes in normal operation.

---

Audit Report

## Title
Trivial-PoW CompactBlock Flooding via Missing `compact_target` Consensus Check in `EpochVerifier` — (`sync/src/relayer/compact_block_process.rs`, `verification/src/header_verifier.rs`)

## Summary
The CompactBlock relay path explicitly bypasses rate limiting on the assumption that PoW imposes a real cost. However, `HeaderVerifier` (used in `contextual_check`) does not validate that the header's `compact_target` matches the consensus-expected epoch target. With `compact_target=0x20ffffff`, the PoW target becomes `U256::max_value()`, making any nonce trivially valid. An attacker can flood `HeaderMap` and the sled on-disk backend with zero real mining cost, exhausting memory and disk, and potentially crashing the node.

## Finding Description

**Rate limit bypass:** In `sync/src/relayer/mod.rs` lines 112–114, CompactBlock messages unconditionally skip the per-peer rate limiter with the comment "CompactBlock will be verified by POW." This assumption is broken.

**PoW check is trivially satisfied:** `PowVerifier::verify` in `pow/src/eaglesong.rs` calls `compact_to_target(header.raw().compact_target())`. With `compact_target=0x20ffffff`, this returns `(U256::max_value(), false)`. Since `U256::max_value()` is the maximum possible value, any Eaglesong output satisfies `output <= block_target`. Any nonce (including `0`) passes.

**`EpochVerifier` in `header_verifier.rs` does not check `compact_target`:** The `EpochVerifier::verify` called from `HeaderVerifier` only checks `is_well_formed()` (length > 0, index < length) and `is_successor_of(parent)` (epoch number/index continuity). It never compares the header's `compact_target` against the consensus-derived expected target for that epoch. An attacker can set any `compact_target` while maintaining valid epoch succession.

**The `compact_target` vs. consensus check only exists in `ContextualBlockVerifier`:** `EpochVerifier` in `contextual_block_verifier.rs` lines 500–507 performs `self.epoch.compact_target() != actual_compact_target`, but this runs only during full block verification via `accept_block` — after `insert_valid_header` has already mutated `HeaderMap`.

**`insert_valid_header` mutates state before full verification:** After `contextual_check` passes (which includes the insufficient `HeaderVerifier`), `shared.insert_valid_header(self.peer, &header)` is called unconditionally at line 78, inserting the fake header into `HeaderMap` with no `compact_target` validation.

**Unbounded chaining:** Once a fake header at height N is in `HeaderMap`, `contextual_check` for height N+1 will find it via `shared.get_header_index_view`, allowing the attacker to build an unbounded chain of trivially-mined headers, each inserted into `HeaderMap`.

**Sled backend overflow:** `HeaderMap` spills to a sled on-disk backend with no size cap once the 256 MB in-memory limit is exceeded. The `limit_memory` task runs every 5 seconds. Continuous flooding exhausts disk space.

## Impact Explanation

This vulnerability allows an unprivileged attacker with a standard P2P connection to exhaust both in-process memory (up to 256 MB of `HeaderMap`) and on-disk storage (unbounded sled backend) on any synced CKB full node. Disk exhaustion causes I/O errors and node crashes. This matches the **High** impact class: *Vulnerabilities which could easily crash a CKB node* (10001–15000 points).

## Likelihood Explanation

- Requires only a standard P2P connection — no privileges, keys, or majority hashpower.
- `compact_target=0x20ffffff` requires zero real work; any nonce satisfies the PoW check.
- The rate limiter is explicitly disabled for this message type.
- The attack is trivially scriptable, repeatable, and locally testable.
- Works against any synced (non-IBD) CKB full node.

## Recommendation

1. **Add `compact_target` validation to `HeaderVerifier`:** The `EpochVerifier` in `verification/src/header_verifier.rs` should compute the expected `compact_target` from the parent's epoch using `consensus.next_epoch_ext()` and reject headers whose `compact_target` does not match. This mirrors the check already present in `contextual_block_verifier.rs`.

2. **Re-enable rate limiting for CompactBlock:** The justification "CompactBlock will be verified by POW" is only valid when PoW imposes a real cost. Since `compact_target` is attacker-controlled and not validated against consensus before the rate-limit bypass, the assumption is broken. Apply a per-peer rate limit to CompactBlock messages.

3. **Add a `compact_target` sanity check in `non_contextual_check`:** Reject headers whose `compact_target` deviates beyond a configurable bound from the current epoch's target, as a cheap early filter.

## Proof of Concept

```python
# Pseudocode — zero real mining cost
parent = get_chain_tip()  # real synced chain tip
for i in range(N):
    header = build_header(
        parent_hash=parent.hash,
        number=parent.number + 1,
        epoch=successor_epoch(parent.epoch),  # valid epoch succession (index+1)
        compact_target=0x20ffffff,             # trivially easy, any nonce works
        timestamp=parent.timestamp + 1000,     # passes TimestampVerifier
        nonce=0,                               # any nonce satisfies PowVerifier
    )
    compact_block = build_compact_block(header, prefilled=[cellbase])
    send_p2p(compact_block)  # no rate limit applied
    parent = header           # use fake header as next parent (found in HeaderMap)
    # Each iteration: HeaderMap grows by one entry; sled backend written after 256 MB
```

After N iterations, `HeaderMap` memory is exhausted and the sled backend grows proportionally. The `compact_target` mismatch is only caught in `ContextualBlockVerifier::EpochVerifier` during full block processing — after `HeaderMap` state has already been mutated and the `insert_valid_header` call has completed.

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
