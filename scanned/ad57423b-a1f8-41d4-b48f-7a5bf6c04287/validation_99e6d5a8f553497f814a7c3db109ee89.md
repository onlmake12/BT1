Audit Report

## Title
`PoolMap::add_entry` Overwrites `total_tx_size`/`total_tx_cycles` With Stale Pre-Computed Values After In-Flight Evictions — (`tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, `updated_stat_for_add_tx` snapshots candidate totals before `check_and_record_ancestors` runs. When `check_and_record_ancestors` evicts entries via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, those decrements to `self.total_tx_size` are immediately overwritten by the stale snapshot on lines 218–219. The result is a permanent overcount of `total_tx_size` equal to the sum of evicted transaction sizes, causing `limit_size` to evict additional valid transactions unnecessarily.

## Finding Description

`add_entry` (lines 200–221) executes in this order:

1. **Line 210–211**: `updated_stat_for_add_tx` reads `self.total_tx_size` and returns `(self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles)` as a snapshot. It does not write to `self.total_tx_size`.
2. **Line 213**: `check_and_record_ancestors` is called. When `ancestors_count > self.max_ancestors_count` and the excess is attributable to `cell_ref_parents` (pool transactions that reference an output being spent by the new tx as a `cell_dep`), it calls `self.remove_entry_and_descendants(next_id)` (line 618). This chains into `remove_entry` (line 247), which calls `update_stat_for_remove_tx`, **directly decrementing `self.total_tx_size` in place** (lines 738–740).
3. **Lines 218–219**: `self.total_tx_size = total_tx_size` overwrites the now-decremented field with the pre-eviction snapshot.

`get_tx_ancenstors` (lines 517–554) confirms that `cell_ref_parents` are populated when the new transaction's input `previous_output` matches an entry in `self.edges.deps` — i.e., when an existing pool transaction declared that same outpoint as a `cell_dep` (lines 531–533). This is the precise trigger condition.

After the overwrite, the invariant is broken:

```
self.total_tx_size (reported) = original + entry.size
self.total_tx_size (correct)  = original − evicted_sizes + entry.size
overcount                     = evicted_sizes
```

`limit_size` (pool.rs line 298) loops while `self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting entries until the condition is false. With an inflated `total_tx_size`, it evicts more entries than the actual pool occupancy warrants, issuing `Reject::Full` for transactions that should have remained.

## Impact Explanation

The concrete impact is that valid, fee-paying transactions are silently dropped from the pool with `Reject::Full` even when the pool has physical capacity. The `tx_pool_info` RPC returns an inflated `total_tx_size`, misleading operators. This constitutes an important correctness defect in the pool's size-accounting invariant, matching the **Low (501–2000 points)** impact class: "Any other important performance improvements for CKB." The bug does not crash the node, cause consensus deviation, or damage the economy, so higher severity tiers are not warranted.

## Likelihood Explanation

The trigger requires: (a) existing pool transactions that declare a specific unspent output as a `cell_dep`, and (b) a new transaction whose input spends that same output, with the resulting `ancestors_count` exceeding `max_ancestors_count` while `cell_ref_parents` is large enough to bring it back within limits. This is an unprivileged `send_transaction` RPC call. The scenario is non-trivial to set up (requires a chain depth near `max_ancestors_count`) but is fully within reach of any external actor and is exercised by existing integration tests for ancestor-count eviction paths.

## Recommendation

Remove the pre-computation of `(total_tx_size, total_tx_cycles)` before `check_and_record_ancestors`. After all mutations are complete, update the live fields directly:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Now self.total_tx_size already reflects any evictions; just add the new entry.
self.total_tx_size = self.total_tx_size
    .checked_add(entry.size)
    .ok_or_else(|| Reject::Full(format!(...)))?;
self.total_tx_cycles = self.total_tx_cycles
    .checked_add(entry.cycles)
    .ok_or_else(|| Reject::Full(format!(...)))?;
```

Optionally add a `debug_assert_eq!(self.total_tx_size, self.recompute_total_stat().unwrap().0)` post-insertion invariant check to catch future drift in tests.

## Proof of Concept

1. Set `max_ancestors_count = N` (e.g., 25). Submit `N` transactions `T1…TN` to the pool where each `Ti` declares outpoint `O` (output of some confirmed on-chain tx `P`) as a `cell_dep`. Each `Ti` is independent (no parent–child relationship among them), so `ancestors_count` for each is 1.
2. Submit a new transaction `T_attack` whose **input** spends outpoint `O`. `get_tx_ancenstors` will find all `T1…TN` as `cell_ref_parents` (via `self.edges.deps`), making `ancestors_count = N + 1 > max_ancestors_count`. The condition `ancestors_count - cell_ref_parents.len() = 1 <= N` is satisfied, so the eviction branch executes.
3. Inside `check_and_record_ancestors`, `remove_entry_and_descendants` is called for each `Ti`, decrementing `self.total_tx_size` by `sum(Ti.size)`. After all evictions, `self.total_tx_size = 0` (assuming pool was otherwise empty).
4. Lines 218–219 then set `self.total_tx_size = 0 + sum(Ti.size) + T_attack.size` — an overcount of `sum(Ti.size)`.
5. `limit_size` is called. Because `total_tx_size` is massively overcounted relative to the single entry `T_attack` actually in the pool, it evicts `T_attack` itself with `Reject::Full`.
6. Verify via `tx_pool_info` RPC that `total_tx_size` is inflated and that `T_attack` (and any other unrelated pending transactions) are rejected despite the pool being physically empty.