Audit Report

## Title
`PoolMap::add_entry` Overwrites `total_tx_size`/`total_tx_cycles` With Stale Pre-Computed Values After In-Flight Evictions — (`tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, `updated_stat_for_add_tx` snapshots candidate totals before `check_and_record_ancestors` runs. When `check_and_record_ancestors` evicts entries via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, it correctly decrements `self.total_tx_size` in place. The subsequent unconditional assignment at lines 218–219 then overwrites that decremented value with the stale pre-eviction snapshot, permanently overcounting `total_tx_size` by the sum of evicted transaction sizes. This causes `limit_size` to evict additional legitimate transactions that would not otherwise have been removed.

## Finding Description

`updated_stat_for_add_tx` is a `&self` (read-only) method that computes and returns `(self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles)` without committing anything to `self`:

```
// pool_map.rs L710-729
fn updated_stat_for_add_tx(&self, tx_size: usize, cycles: Cycle) -> Result<(usize, Cycle), Reject>
```

The returned values are stored in local variables `total_tx_size` and `total_tx_cycles` at lines 210–211.

`check_and_record_ancestors` (lines 588–640) enters the eviction branch when `ancestors_count > self.max_ancestors_count` AND `cell_ref_parents` exist that can be removed to bring the count within limits (line 603). Inside that branch it calls `remove_entry_and_descendants` (line 618), which calls `remove_entry` (line 263), which calls `update_stat_for_remove_tx` (line 247). `update_stat_for_remove_tx` mutates `self.total_tx_size` in place (lines 738–740):

```
self.total_tx_size = total_tx_size;   // decremented by evicted size
self.total_tx_cycles = total_tx_cycles;
```

After `check_and_record_ancestors` returns, `self.total_tx_size` correctly equals `original − evicted_sizes`. Lines 218–219 then unconditionally overwrite it:

```
self.total_tx_size = total_tx_size;   // stale: original + entry.size
self.total_tx_cycles = total_tx_cycles;
```

Net result:
- Correct value: `original − evicted_sizes + entry.size`
- Stored value: `original + entry.size`
- Overcount: `evicted_sizes`

`cell_ref_parents` are populated at lines 529–534 of `get_tx_ancenstors`: any pool transaction that lists an output as a `cell_dep` where the incoming transaction spends that same output as an input becomes a `cell_ref_parent` and is also added to `parents`, contributing to `ancestors_count`. This is the precise trigger path.

The caller in `process.rs` (lines 136–147) treats the returned `evicts` as already-removed entries and only calls `call_reject` on them — confirming the entries were removed inside `add_entry`, not by the caller. `limit_size` is then called at line 151 using the now-overcounted `self.total_tx_size` as its loop guard (pool.rs line 298).

## Impact Explanation

`limit_size` loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size` (pool.rs line 298), evicting lowest-fee-rate transactions until the condition is false. An overcounted `total_tx_size` causes this loop to run more iterations than necessary, evicting legitimate transactions with valid fee rates and issuing `Reject::Full` for them. The `tx_pool_info` RPC also reports the inflated value, misleading operators. During reorg (`_update_tx_pool_for_reorg`), `limit_size` is called with `current_entry_id = None`, meaning the overcount can cause arbitrary pending/proposed transactions to be evicted on every reorg. This matches the allowed impact: **Low (501–2000 points) — any other important performance/correctness issue for CKB**, with potential escalation to **Note** for the RPC reporting aspect.

## Likelihood Explanation

The trigger requires: (1) enough pool transactions that reference a specific on-chain output as a `cell_dep` such that their count plus 1 exceeds `max_ancestors_count` (default 25); (2) a new transaction whose input spends that same output. Both conditions are reachable via standard unprivileged `send_transaction` RPC calls. An attacker can pre-seed the pool with 25+ transactions sharing a common `cell_dep` output, then submit a spending transaction to trigger the overcount. The scenario is specific but requires no special privileges and is repeatable.

## Recommendation

Remove the pre-computation of `(total_tx_size, total_tx_cycles)` before `check_and_record_ancestors`. After all mutations (evictions + insertion) are complete, update the live fields directly:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Update totals from the live (already-eviction-adjusted) self.total_tx_size:
self.total_tx_size = self.total_tx_size.checked_add(entry.size)
    .ok_or_else(|| Reject::Full(format!("total_tx_size overflow")))?;
self.total_tx_cycles = self.total_tx_cycles.checked_add(entry.cycles)
    .ok_or_else(|| Reject::Full(format!("total_tx_cycles overflow")))?;
```

Optionally add a `debug_assert_eq!(self.total_tx_size, self.recompute_total_stat().unwrap().0)` post-insertion invariant check to catch future drift in tests.

## Proof of Concept

1. Set `max_ancestors_count = 25` (default). Pre-populate the pool with 25 transactions `T1…T25`, each declaring `cell_dep` on output `O` of an on-chain transaction `P`. None of `T1…T25` spend `O` as an input, so they are all valid pool entries. Each has size `S`.
2. Submit `T_attack` whose single input spends output `O` of `P`. In `get_tx_ancenstors`, `T1…T25` are added to both `cell_ref_parents` and `parents`; `ancestors_count = 26 > 25`.
3. `check_and_record_ancestors` enters the eviction branch (line 603 passes since `26 - 25 = 1 ≤ 25`). It calls `remove_entry_and_descendants` for enough of `T1…T25` to bring `ancestors_count ≤ 25`, decrementing `self.total_tx_size` by `evicted_count × S`.
4. Lines 218–219 overwrite `self.total_tx_size` with `25×S + T_attack.size` instead of the correct `(25 - evicted_count)×S + T_attack.size`.
5. `limit_size` is called. Because `total_tx_size` is overcounted by `evicted_count × S`, it evicts `evicted_count` additional legitimate transactions from the pool.
6. Verify via `tx_pool_info` RPC that `total_tx_size` is inflated and that valid transactions are rejected with `Reject::Full`.