Audit Report

## Title
Stale `total_tx_size`/`total_tx_cycles` Overwrite After Ancestor Eviction Inflates Pool Size Accounting — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` captures pre-eviction totals into local variables before `check_and_record_ancestors` runs. When `check_and_record_ancestors` evicts transactions, `update_stat_for_remove_tx` correctly decrements `self.total_tx_size`/`self.total_tx_cycles` in place. However, `add_entry` then unconditionally overwrites those fields with the stale pre-eviction locals, permanently inflating the counters by the size of the evicted transactions. This causes `limit_size` to evict valid fee-paying transactions and causes future submissions to be rejected with `Reject::Full` when real capacity exists.

## Finding Description
`updated_stat_for_add_tx` (lines 711–729) reads `self.total_tx_size` at call time and returns `(self.total_tx_size + tx_size, self.total_tx_cycles + cycles)` as local variables without mutating state.

In `add_entry` (lines 210–219):
- Lines 210–211 snapshot the pre-eviction totals into locals `total_tx_size` and `total_tx_cycles`.
- Line 213 calls `check_and_record_ancestors`, which at line 618 calls `remove_entry_and_descendants` when `ancestors_count > self.max_ancestors_count` and `cell_ref_parents` are non-empty.
- `remove_entry_and_descendants` (lines 252–265) calls `remove_entry` for each removed tx, which at line 247 calls `update_stat_for_remove_tx`, which at lines 738–740 directly decrements `self.total_tx_size` in place.
- Lines 218–219 then unconditionally write the stale locals back to `self.total_tx_size` and `self.total_tx_cycles`, erasing all decrements made during eviction.

The correct post-eviction value would be `S + new_size - evicted_size`; the written value is `S + new_size`, inflating by exactly `evicted_size`. No reconciliation step exists between line 213 and lines 218–219.

## Impact Explanation
`limit_size` in `tx-pool/src/pool.rs` (line 298) loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`. With an inflated counter, this loop evicts valid, fee-paying transactions that would otherwise remain. Subsequent calls to `updated_stat_for_add_tx` also start from the inflated baseline, causing `Reject::Full` for transactions that fit within the real pool capacity. An attacker who repeatedly triggers the eviction path keeps the effective pool size artificially small, causing legitimate transactions to be continuously rejected or evicted. This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
The eviction path in `check_and_record_ancestors` requires: (1) a new transaction whose cell dep is already consumed by an in-pool transaction (`cell_ref_parents` non-empty), and (2) total ancestor count exceeding `max_ancestors_count` (default 25). Both conditions are reachable by any unprivileged caller via the `send_transaction` RPC or a relay peer. An attacker who observes mempool state via `get_raw_tx_pool` can craft transactions that reliably satisfy these conditions. The inflation is permanent per trigger and accumulates across repeated submissions, making the attack low-cost and repeatable.

## Recommendation
Move `updated_stat_for_add_tx` to after `check_and_record_ancestors` completes, so the snapshot is taken from the already-decremented `self.total_tx_size`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size  = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, remove the local variables entirely and apply the increment in-place after all mutations complete: `self.total_tx_size += entry.size` with overflow handling.

## Proof of Concept
1. Fill the pool with a chain of 24 transactions `T1 → T2 → … → T24` where each spends the previous output.
2. Submit transaction `R` that takes `T1`'s output as a **cell dep** (making `R` a `cell_ref_parent`). Record pool `total_tx_size = S` and `R`'s size as `S_R`.
3. Submit `T_new` spending `T1`'s output. `T_new` now has 24 ancestors + `R` as a cell-ref parent, exceeding `max_ancestors_count = 25`.
4. `check_and_record_ancestors` evicts `R` via `remove_entry_and_descendants` → `update_stat_for_remove_tx`, correctly setting `self.total_tx_size = S - S_R`.
5. `add_entry` then writes `self.total_tx_size = S + size(T_new)`, inflating by `S_R`.
6. Verify via `get_tip_tx_pool_info` that reported `total_tx_size` exceeds the sum of actual entry sizes by `S_R`.
7. Repeat steps 1–6 to accumulate inflation until `limit_size` evicts a transaction that fits within `max_tx_pool_size`, or until `updated_stat_for_add_tx` rejects a valid submission with `Reject::Full`.