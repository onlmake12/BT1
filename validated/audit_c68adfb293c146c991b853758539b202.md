Audit Report

## Title
Stale `total_tx_size`/`total_tx_cycles` Overwrite After In-Flight Eviction in `add_entry` Inflates Pool Accounting — (File: tx-pool/src/component/pool_map.rs)

## Summary
`PoolMap::add_entry` snapshots new `total_tx_size`/`total_tx_cycles` values before calling `check_and_record_ancestors`, which can internally evict transactions via `remove_entry_and_descendants` and correctly decrement those counters. After eviction, `add_entry` unconditionally overwrites the counters with the stale pre-eviction snapshot, permanently inflating them by the aggregate size/cycles of every evicted transaction. The inflated counters cause `limit_size` to over-evict legitimate transactions with `Reject::Full` errors even when the pool has physical capacity.

## Finding Description
In `add_entry` (lines 200–221 of `tx-pool/src/component/pool_map.rs`):

1. **Line 210–211**: `updated_stat_for_add_tx` computes `self.total_tx_size + entry.size` and stores the result in a local `(total_tx_size, total_tx_cycles)` snapshot. At this point, no eviction has occurred.

2. **Line 213**: `check_and_record_ancestors` is called. When `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (lines 603–625), it calls `remove_entry_and_descendants` in a loop. Each call to `remove_entry_and_descendants` invokes `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size` and `self.total_tx_cycles` (lines 733–758).

3. **Lines 218–219**: `add_entry` unconditionally assigns `self.total_tx_size = total_tx_size` and `self.total_tx_cycles = total_tx_cycles`, overwriting the correctly-decremented values with the stale pre-eviction snapshot. The net effect is that `self.total_tx_size` is inflated by exactly the sum of the evicted transactions' sizes.

`limit_size` (pool.rs line 298) loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, so the inflated counter causes it to evict additional legitimate transactions that would otherwise remain in the pool.

The `updated_stat_for_add_tx` function (lines 711–729) is a pure read that returns `self.total_tx_size + entry.size` without modifying state, so the snapshot is always stale if any eviction occurs between lines 211 and 218.

## Impact Explanation
An attacker can repeatedly trigger the eviction branch in `check_and_record_ancestors` to progressively inflate `total_tx_size`. Each trigger causes `limit_size` to over-evict legitimate pending/proposed transactions with `Reject::Full`. Over repeated triggering, the effective pool capacity shrinks toward zero, preventing legitimate transactions from entering the mempool. This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs** — the pool becomes progressively unable to accept legitimate transactions, degrading transaction throughput across the network.

## Likelihood Explanation
The eviction branch requires a transaction whose ancestor count exceeds `max_ancestors_count` (default 1,000) but whose excess ancestors are all cell-dep-referencing parents. An unprivileged user reachable via `send_transaction` RPC or P2P relay can craft such a chain. Building a 1,000-tx chain has a cost in fees, but each successful trigger permanently inflates the counters, so the attacker's investment compounds over time. The bug also fires in non-adversarial conditions whenever organic chains hit this path.

## Recommendation
Remove the pre-computed snapshot and instead apply the addition directly to `self.total_tx_size`/`self.total_tx_cycles` after `check_and_record_ancestors` completes, so the increment is applied to the post-eviction state:

```rust
// Remove lines 210-211 (the pre-snapshot)
// After all mutations, replace lines 218-219 with:
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

The overflow check currently performed by `updated_stat_for_add_tx` (which returns `Reject::Full` on overflow) should be retained as a pre-flight validation before any mutations occur, but its returned values should be discarded rather than committed.

## Proof of Concept
1. Build a chain of 999 transactions `T1 → T2 → … → T999` where each `Ti` is a cell-dep parent of `T1000`, making `T1000`'s ancestor count = 1,000 via cell-dep references.
2. Submit `T1000`. `check_and_record_ancestors` detects `ancestors_count = 1,001 > max_ancestors_count = 1,000` but `1,001 - 999 = 2 ≤ 1,000`, so it evicts cell-dep parents (e.g., `T999`, size S) via `remove_entry_and_descendants`, correctly decrementing `self.total_tx_size` by S.
3. `add_entry` lines 218–219 overwrite `self.total_tx_size` with the pre-eviction snapshot, inflating it by S.
4. `limit_size` is called immediately after (process.rs line 151); it sees `total_tx_size > max_tx_pool_size` and evicts one additional legitimate transaction.
5. Repeat: each submission that triggers the eviction branch inflates `total_tx_size` by the evicted size, causing `limit_size` to cascade-evict additional legitimate transactions until the pool is effectively unusable.