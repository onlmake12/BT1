Audit Report

## Title
Stale Pre-Computed `total_tx_size`/`total_tx_cycles` Overwrites Post-Eviction Decrements in `add_entry` — (`tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` snapshots the new totals at lines 210–211 before `check_and_record_ancestors` runs at line 213. When `check_and_record_ancestors` evicts transactions via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, those decrements to `self.total_tx_size`/`self.total_tx_cycles` are immediately overwritten at lines 218–219 by the stale pre-eviction snapshot. Each eviction event permanently inflates the counters by the aggregate size/cycles of the evicted transactions, eventually causing every subsequent `add_entry` call to return `Reject::Full` and freezing the pool.

## Finding Description
The exact sequence in `add_entry` (lines 200–221):

1. **Line 210–211**: `updated_stat_for_add_tx(entry.size, entry.cycles)` computes `total_tx_size = self.total_tx_size + entry.size` and `total_tx_cycles = self.total_tx_cycles + entry.cycles` as a snapshot against the *pre-eviction* live counters.
2. **Line 213**: `check_and_record_ancestors` may enter the eviction branch at lines 603–625 when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. It calls `remove_entry_and_descendants` in a loop, which calls `remove_entry` (line 235–249), which calls `update_stat_for_remove_tx` (line 247), correctly decrementing `self.total_tx_size` and `self.total_tx_cycles`.
3. **Lines 218–219**: The stale snapshot from step 1 is unconditionally written back, erasing all decrements applied in step 2.

The result: after each eviction event, `total_tx_size` is inflated by exactly the sum of the evicted transactions' sizes (and similarly for cycles). `updated_stat_for_add_tx` uses `self.total_tx_size` as the base for `checked_add` (lines 716–721), so once the inflated value reaches `usize::MAX` or the pool's configured size limit, every future call returns `Reject::Full`.

The FIXME comment at lines 583–587 acknowledges that rollback after eviction-then-failure is not handled, confirming the code path is known to be incomplete.

## Impact Explanation
`total_tx_size` and `total_tx_cycles` are the authoritative pool-capacity counters. After sufficient eviction events, they diverge upward from reality and the pool permanently rejects all new transaction submissions with `Reject::Full`, even though physical capacity exists. This constitutes a **tx-pool denial-of-service** matching the allowed High impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* An attacker can trigger this repeatedly at negligible cost (standard valid transaction submissions), and the inflation accumulates monotonically until the pool is frozen, requiring a node restart to recover.

## Likelihood Explanation
The eviction path is reachable by any unprivileged user. The attacker needs only to submit transactions that share a common cell dep (making them `cell_ref_parents` of each other) and then submit a new transaction whose ancestor count exceeds `max_ancestors_count` but whose excess is covered by those `cell_ref_parents`. This is a standard, valid transaction submission flow requiring no privileged keys, no majority hashpower, and no social engineering. The condition is explicitly documented in the code. Repeated triggering accumulates inflation monotonically.

## Recommendation
Move the stat update to **after** `check_and_record_ancestors` completes, so that eviction decrements are not overwritten. Remove the pre-computed snapshot pattern and instead apply an incremental add after all evictions have settled:

```rust
// Remove lines 210-211 (the pre-computed snapshot)
// After insert_entry / record_entry_descendants, replace lines 218-219 with:
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

The overflow/capacity check (`updated_stat_for_add_tx`) should be moved to after evictions complete, operating on the post-eviction `self.total_tx_size`/`self.total_tx_cycles`.

## Proof of Concept
1. Configure a node with `max_ancestors_count = 25`.
2. Submit 25 transactions `A₁…A₂₅` that all reference the same cell dep `C` (making them mutual `cell_ref_parents`). Record their individual sizes as `s`.
3. Submit a new transaction `T` that spends an output of `A₁` and also references cell dep `C`. `check_and_record_ancestors` sees `ancestors_count = 26 > 25`, but `26 - 25 = 1 ≤ 25`, so it evicts `A₁` via `remove_entry_and_descendants`, calling `update_stat_for_remove_tx(s, ...)` which decrements `self.total_tx_size` by `s`. Then `add_entry` writes back `total_tx_size = old_total + T.size` (the stale snapshot), re-inflating by `s`.
4. Repeat step 3 with fresh transactions. After `N` iterations, `total_tx_size` is inflated by `N × s` bytes above reality.
5. Once the inflated value causes `checked_add` to overflow or exceed the pool's configured size limit, `updated_stat_for_add_tx` returns `Reject::Full` for every subsequent submission, freezing the pool until node restart.