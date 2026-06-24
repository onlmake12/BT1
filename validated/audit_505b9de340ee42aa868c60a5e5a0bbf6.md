The code confirms the claim. Let me verify the complete picture before rendering a verdict.

Audit Report

## Title
`PoolMap::add_entry` Overwrites `total_tx_size`/`total_tx_cycles` With Stale Pre-Computed Values After In-Flight Evictions — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

`add_entry` snapshots `(total_tx_size, total_tx_cycles)` before calling `check_and_record_ancestors`. When that call evicts entries via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, the live decrements to `self.total_tx_size` are immediately overwritten by the stale snapshot at lines 218–219. The result is a permanent overcount of `total_tx_size` equal to the sum of evicted transaction sizes, causing `limit_size` to evict additional valid transactions with `Reject::Full` even when the pool has physical capacity.

## Finding Description

**Root cause — `add_entry` (lines 200–221):**

- **Lines 210–211**: `updated_stat_for_add_tx` is a read-only function (lines 711–729) that returns `(self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles)` as a local snapshot. It does not write to any field.
- **Line 213**: `check_and_record_ancestors` is called. When `ancestors_count > self.max_ancestors_count` and `ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count` (line 603), the eviction loop at lines 616–625 calls `self.remove_entry_and_descendants(next_id)` (line 618). `remove_entry_and_descendants` calls `remove_entry` for each removed id (line 263), and `remove_entry` calls `self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles)` (line 247), which **directly writes** `self.total_tx_size = total_tx_size` (line 739) — a live, in-place decrement.
- **Lines 218–219**: `self.total_tx_size = total_tx_size` and `self.total_tx_cycles = total_tx_cycles` unconditionally overwrite the now-correctly-decremented live fields with the pre-eviction snapshot, producing:

```
self.total_tx_size (reported) = original + entry.size
self.total_tx_size (correct)  = original − evicted_sizes + entry.size
overcount                     = evicted_sizes
```

**Trigger condition — `get_tx_ancenstors` (lines 517–554):**

`cell_ref_parents` is populated at lines 531–533 when the new transaction's input `previous_output` matches an outpoint recorded in `self.edges.deps` — i.e., when existing pool transactions declared that same outpoint as a `cell_dep`. This is a normal, reachable pool state.

**Downstream effect — `limit_size` (pool.rs lines 292–329):**

The loop at line 298 runs `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`. With an inflated `total_tx_size`, it evicts entries that should remain, issuing `Reject::Full` for valid, fee-paying transactions.

No existing guard corrects the overwrite: `update_stat_for_remove_tx` has an underflow fallback that calls `recompute_total_stat` (lines 743–749), but this path is only reached on underflow, not on the overwrite that occurs here.

## Impact Explanation

Valid, fee-paying transactions are silently dropped from the pool with `Reject::Full` even when the pool has physical capacity. The `tx_pool_info` RPC returns an inflated `total_tx_size`, misleading node operators. This is a concrete correctness defect in the pool's size-accounting invariant. It maps to **Low (501–2000 points): "Any other important performance improvements for CKB."** The bug does not crash the node, cause consensus deviation, or damage the economy.

## Likelihood Explanation

The trigger requires: (a) existing pool transactions that declare a specific unspent output as a `cell_dep`, and (b) a new transaction whose input spends that same output, with the resulting `ancestors_count` exceeding `max_ancestors_count` while `cell_ref_parents` is large enough to bring it back within limits after eviction. This is reachable via an unprivileged `send_transaction` RPC call. The setup is non-trivial but fully within reach of any external actor and is exercised by existing integration tests for ancestor-count eviction paths.

## Recommendation

Remove the pre-computation of `(total_tx_size, total_tx_cycles)` before `check_and_record_ancestors`. After all mutations are complete, update the live fields directly:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// self.total_tx_size already reflects any evictions; just add the new entry.
self.total_tx_size = self.total_tx_size
    .checked_add(entry.size)
    .ok_or_else(|| Reject::Full(format!(...)))?;
self.total_tx_cycles = self.total_tx_cycles
    .checked_add(entry.cycles)
    .ok_or_else(|| Reject::Full(format!(...)))?;
```

Optionally add a `debug_assert_eq!(self.total_tx_size, self.recompute_total_stat().unwrap().0)` post-insertion invariant check in tests to catch future drift.

## Proof of Concept

1. Set `max_ancestors_count = N` (e.g., 25). Submit `N` independent transactions `T1…TN` to the pool where each `Ti` declares outpoint `O` (an output of a confirmed on-chain transaction `P`) as a `cell_dep`. Each `Ti` has `ancestors_count = 1`.
2. Submit a new transaction `T_attack` whose **input** spends outpoint `O`. `get_tx_ancenstors` finds all `T1…TN` as `cell_ref_parents` via `self.edges.deps` (lines 531–533), making `ancestors_count = N + 1 > max_ancestors_count`. The condition at line 603 is satisfied (`N + 1 - N = 1 <= N`), so the eviction loop executes.
3. Inside `check_and_record_ancestors`, `remove_entry_and_descendants` is called for each `Ti`, decrementing `self.total_tx_size` by `sum(Ti.size)`. After all evictions, `self.total_tx_size = 0` (assuming pool was otherwise empty).
4. Lines 218–219 then set `self.total_tx_size = 0 + sum(Ti.size) + T_attack.size` — an overcount of `sum(Ti.size)`.
5. `limit_size` is called. Because `total_tx_size` is massively overcounted relative to the single entry `T_attack` actually in the pool, it evicts `T_attack` itself with `Reject::Full`.
6. Verify via `tx_pool_info` RPC that `total_tx_size` is inflated and that `T_attack` (and any other unrelated pending transactions) are rejected despite the pool being physically empty.