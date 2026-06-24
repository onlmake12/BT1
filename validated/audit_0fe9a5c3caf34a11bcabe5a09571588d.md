Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Stale Snapshot Overwrite After In-Flight Evictions in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, a pre-eviction snapshot of `total_tx_size`/`total_tx_cycles` is captured before `check_and_record_ancestors` runs. If `check_and_record_ancestors` evicts cell-ref-parent transactions, `update_stat_for_remove_tx` directly decrements `self.total_tx_size`/`self.total_tx_cycles`. The stale pre-eviction snapshot is then unconditionally written back at lines 218–219, permanently overwriting those decrements. Each eviction event inflates `total_tx_size` by the aggregate size of all evicted transactions, causing `limit_size` to over-evict legitimate pool entries on every subsequent insertion.

## Finding Description

The exact sequence in `add_entry` (lines 200–221) is confirmed by the code:

- **Lines 210–211**: `updated_stat_for_add_tx` is a pure read (`&self`) that returns `self.total_tx_size + tx_size` without mutating state. The result is stored in local variables `(total_tx_size, total_tx_cycles)`.
- **Line 213**: `check_and_record_ancestors` is called. When `ancestors_count > max_ancestors_count` and the excess is attributable to `cell_ref_parents`, it enters the eviction loop at lines 615–625, calling `remove_entry_and_descendants` for each candidate. Each call chains into `update_stat_for_remove_tx`, which **directly writes** `self.total_tx_size -= evicted_size` and `self.total_tx_cycles -= evicted_cycles`.
- **Lines 218–219**: The stale local snapshot (computed before any evictions) is unconditionally assigned back to `self.total_tx_size` and `self.total_tx_cycles`, overwriting all decrements applied during eviction.

The arithmetic consequence:

```
Correct post-eviction value:  old_total − Σ(evicted_sizes) + new_entry_size
Written back value:           old_total + new_entry_size
Permanent inflation per call: Σ(evicted_sizes)
```

No guard or reconciliation step exists between lines 213 and 218 to re-read the current (post-eviction) value of `self.total_tx_size` before the write-back.

## Impact Explanation

`limit_size` (pool.rs lines 297–327) loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting lowest-fee-rate entries until the condition is false. Because `total_tx_size` is inflated by `Σ(evicted_sizes)` on every triggering insertion, repeated rounds accumulate inflation until `total_tx_size` permanently exceeds `max_tx_pool_size` even when the pool is nearly empty. At that point `limit_size` continuously evicts legitimate transactions, denying pool admission to honest users. This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged RPC caller or P2P relay peer. No key material, special privilege, or majority hash power is required. The attacker submits a batch of transactions sharing a common `cell_dep` output, then submits a transaction that consumes that output as an input, triggering the cell-ref-parent eviction loop. The attack is repeatable: each round inflates `total_tx_size` by a controlled amount, and rounds can be chained until the counter exceeds `max_tx_pool_size`.

## Recommendation

Remove the pre-eviction snapshot assignment and instead apply the addition to the **current** (post-eviction) value of `self.total_tx_size` after all mutations complete:

```rust
// Keep only the overflow check; discard the returned snapshot:
self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Apply addition to the live (post-eviction) counters:
self.total_tx_size = self.total_tx_size
    .checked_add(entry.size)
    .ok_or_else(|| Reject::Full(...))?;
self.total_tx_cycles = self.total_tx_cycles
    .checked_add(entry.cycles)
    .ok_or_else(|| Reject::Full(...))?;
```

Alternatively, call `recompute_total_stat()` after all mutations, though this is O(n) in pool size.

## Proof of Concept

1. Submit `N` transactions (`tx_ref_1 … tx_ref_N`) each referencing `cell_X` as a `cell_dep`. All are accepted; `total_tx_size` grows normally.
2. Submit `tx_consume` spending `cell_X` as an **input**. `check_and_record_ancestors` finds `N` cell-ref parents exceeding `max_ancestors_count` and evicts `k = N − max_ancestors_count` of them via `remove_entry_and_descendants` → `update_stat_for_remove_tx`. `self.total_tx_size` is decremented by `Σ size(evicted_k)`.
3. Lines 218–219 write back `total_tx_size = pre_eviction_snapshot + size(tx_consume)`, overwriting the decrements. `total_tx_size` is now inflated by `Σ size(evicted_k)`.
4. Call `tx_pool_info` RPC: `total_tx_size` reports a value larger than the sum of sizes of all entries actually in the pool.
5. Repeat steps 1–3 to accumulate inflation until `total_tx_size > max_tx_pool_size` even though the pool is nearly empty. Subsequent `limit_size` calls evict legitimate transactions, denying pool service.