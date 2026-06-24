Audit Report

## Title
O(N) Full Pool Scan in `remove_expired` Under Write-Lock on Every Block — (`tx-pool/src/pool.rs`)

## Summary
On every accepted block, `remove_expired` unconditionally iterates all entries in the tx-pool via `pool_map.iter()` while holding the tx-pool write-lock. An unprivileged attacker who fills the pool with many small, valid, low-fee transactions can force O(N_total) CPU work and write-lock hold time on every block, degrading concurrent RPC operations proportional to pool occupancy. The `get_by_status` loops in `_update_tx_pool_for_reorg` use the `MultiIndexMap` hash index and are O(N_status), not a full scan as claimed, but `remove_expired` is a genuine unbounded full scan.

## Finding Description

**Root cause — `remove_expired` (`tx-pool/src/pool.rs:271-288`)**

`pool_map.iter()` walks every entry in the pool regardless of status or count:

```rust
let removed: Vec<_> = self
    .pool_map
    .iter()          // ← full scan of ALL entries
    .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
    .map(|entry| entry.inner.clone())
    .collect();
```

This is called unconditionally at `process.rs:1110` inside `_update_tx_pool_for_reorg`, which runs under the tx-pool write-lock acquired at `process.rs:836`. `limit_size` (which enforces the byte-size cap) runs only *after* this scan at `process.rs:1113`, so the full O(N) cost is paid before any eviction.

**Correction to the claim:** The `get_by_status(&Status::Gap)` and `get_by_status(&Status::Pending)` calls at `process.rs:1065` and `1072` use the `#[multi_index(hashed_non_unique)]` index on `status` in `MultiIndexPoolEntryMap` (`pool_map.rs:52-53`), returning only entries of that status. They are O(N_status), not O(N_total), and are additionally gated by `mine_mode` (`process.rs:1061`). The full-scan claim for those loops is overstated.

**Call chain (triggered by any block relay):**
```
P2P peer relays block
  → Relayer::accept_block
    → ChainController::process_block
      → TxPoolController::update_tx_pool_for_reorg
        → process::update_tx_pool_for_reorg (write-lock acquired at line 836)
          → _update_tx_pool_for_reorg
            → tx_pool.remove_expired(...)   ← O(N_total) full scan, unconditional
            → tx_pool.limit_size(...)       ← eviction runs AFTER the scan
```

**Attacker setup:** Submit many small valid transactions via RPC or P2P to fill the pool to `max_tx_pool_size` (180 MB default). With ~200-byte minimum transactions, this yields up to ~900,000 pool entries. Every block relay re-triggers the full scan while the pool remains full.

## Impact Explanation

The write-lock is held for the duration of the `remove_expired` full scan on every block (~10-second cadence). Concurrent `send_transaction` and `get_block_template` RPC calls stall for the lock duration. The node continues to function and no consensus failure or fund loss occurs. This is a local node performance degradation issue matching the allowed impact: **Low (501–2000 points) — Any other important performance improvements for CKB.**

The claim's "Medium" severity is overstated: the actual per-block iteration cost in Rust for hundreds of thousands of simple struct entries is in the low-milliseconds range, not seconds, and the impact is bounded to RPC latency spikes rather than node crash or network-wide congestion.

## Likelihood Explanation

Any unprivileged user with RPC or P2P access can submit transactions. With the default `min_fee_rate` of 1000 shannons/KB, filling 180 MB costs approximately 1.8 CKB — a low but non-zero ongoing cost. The trigger (block relay) fires automatically every ~10 seconds. The attacker must continuously replenish the pool as transactions expire (12-hour default expiry). Feasible but requires sustained fee expenditure.

## Recommendation

1. **Use a timestamp-sorted index for `remove_expired`**: Maintain a `BTreeMap<timestamp, ProposalShortId>` so only entries near the expiry boundary are visited, reducing the per-block scan from O(N_total) to O(expired_count).
2. **Enforce a pool entry count limit** in addition to the byte-size limit to bound worst-case iteration depth independently of transaction size.
3. **Move `remove_expired` to a periodic background task** outside the write-lock critical path, releasing the lock between batches.

## Proof of Concept

1. Configure a CKB node with default `max_tx_pool_size = 180_000_000`.
2. Submit ~50,000 valid minimum-fee transactions via repeated `send_transaction` RPC calls until the pool is at capacity.
3. Relay or mine a new block to the target node.
4. Instrument `remove_expired` with timing: observe that the write-lock is held for the full duration of the `pool_map.iter()` scan over all 50,000+ entries.
5. Concurrently issue `send_transaction` or `get_block_template` RPC calls and measure stall duration — calls will block for the lock duration, confirming service degradation.
6. Repeat every ~10 seconds as new blocks arrive; degradation persists as long as the pool remains full.