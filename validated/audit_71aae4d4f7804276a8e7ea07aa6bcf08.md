Audit Report

## Title
Stale Pre-Eviction `total_tx_size`/`total_tx_cycles` Overwrites Correct Post-Eviction Values in `add_entry` — (File: tx-pool/src/component/pool_map.rs)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` snapshots `self.total_tx_size + entry.size` into local variables before `check_and_record_ancestors` runs. When `check_and_record_ancestors` evicts transactions via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, those decrements correctly update `self.total_tx_size`. However, lines 218–219 then unconditionally overwrite those fields with the stale pre-eviction locals, inflating `total_tx_size` by exactly the total size of all evicted transactions. This causes `limit_size` to evict additional legitimate transactions that should have remained in the pool.

## Finding Description
**Root cause — `tx-pool/src/component/pool_map.rs`, lines 200–221:**

Lines 210–211 call `updated_stat_for_add_tx`, which computes `self.total_tx_size + entry.size` and stores it in a local variable — this is a snapshot taken before any eviction occurs.

Line 213 calls `check_and_record_ancestors`, which at lines 615–625 may call `remove_entry_and_descendants` in a loop to bring the ancestor count below `max_ancestors_count`. Each call chains to `remove_entry` (line 247), which calls `update_stat_for_remove_tx` (line 247), correctly decrementing `self.total_tx_size` and `self.total_tx_cycles` for each evicted transaction.

After all evictions complete, `self.total_tx_size = S - E` (where `S` is the pre-add pool size and `E` is total evicted size). Lines 218–219 then unconditionally assign the stale local `total_tx_size = S + entry.size` back to `self.total_tx_size`, leaving an overestimate of `E`.

`update_stat_for_remove_tx` (lines 733–758) has an underflow fallback via `recompute_total_stat`, but this path is never reached here because the overwrite happens after all removals complete — the subtraction does not underflow, so the fallback is never triggered.

**Accounting:**

| Step | `self.total_tx_size` | Correct value |
|---|---|---|
| Before `add_entry` | `S` | `S` |
| After evicting N txs (total size `E`) | `S - E` | `S - E` |
| After line 218 overwrites | `S + entry.size` | `S - E + entry.size` |

## Impact Explanation
`limit_size` in `pool.rs` line 298 loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting lowest-fee-rate transactions until the condition is false. With `total_tx_size` overestimated by `E`, `limit_size` evicts additional legitimate pending/proposed transactions that would not have been evicted under correct accounting. An attacker can repeat this pattern to continuously drain legitimate transactions from the pool at a cost proportional to the number of crafted cell-dep-referencing transactions. This matches: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, because the attacker can repeatedly trigger pool drain cycles that force legitimate users' transactions out, degrading mempool utility across the network.

## Likelihood Explanation
The eviction path in `check_and_record_ancestors` fires when a new transaction references a cell dep output already referenced by many in-pool transactions (`cell_ref_parents`), and the resulting ancestor count exceeds `max_ancestors_count` (default 125). An unprivileged submitter can craft this scenario by submitting 126+ transactions that all reference the same cell dep output, then submitting a transaction that consumes that output as an input — a valid, standard transaction pattern requiring no special privileges. The attack is repeatable: after eviction, the attacker resubmits the cell-dep-referencing transactions and repeats. The cost per attack cycle is the fees for ~126 transactions.

## Recommendation
Move the stat update to after `check_and_record_ancestors` completes, so evictions are already reflected in `self.total_tx_size` before the new entry's contribution is added:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Correct: add only the new entry's contribution to the already-eviction-adjusted totals
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

The overflow check currently in `updated_stat_for_add_tx` should be retained but moved to after `check_and_record_ancestors`, using the post-eviction `self.total_tx_size` as the baseline.

## Proof of Concept
1. Configure a node with default `max_ancestors_count = 125` and `max_tx_pool_size` near capacity.
2. Submit 200 transactions `T_1..T_200`, each referencing the same cell dep output `O` (none spend `O` as an input). Pool `total_tx_size ≈ 200 * S_tx`.
3. Submit a new transaction `T_new` that spends output `O` as an input, making all 200 transactions `cell_ref_parents` of `T_new`.
4. Inside `add_entry` for `T_new`: `updated_stat_for_add_tx` stores `local = 200*S_tx + S_new`; `check_and_record_ancestors` evicts 76 transactions (to bring ancestor count to 125), each calling `update_stat_for_remove_tx`; `self.total_tx_size` drops to `124*S_tx`; lines 218–219 overwrite: `self.total_tx_size = 200*S_tx + S_new`.
5. Observe `self.total_tx_size` is now `200*S_tx + S_new` instead of the correct `124*S_tx + S_new`.
6. Call `limit_size`; it sees the inflated value and evicts the remaining 124 legitimate transactions unnecessarily.
7. Verify by adding a unit test that asserts `pool_map.total_tx_size` equals the sum of sizes of all entries actually present in the pool after `add_entry` completes, using `recompute_total_stat` as the ground truth.