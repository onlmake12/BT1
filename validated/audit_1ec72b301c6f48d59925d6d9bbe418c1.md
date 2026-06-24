Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Overwritten After Cell-Ref-Parent Eviction in `add_entry` — (File: tx-pool/src/component/pool_map.rs)

## Summary
In `PoolMap::add_entry`, pool-wide size and cycle statistics are pre-computed into local variables before `check_and_record_ancestors` runs. When that function evicts cell-ref-parent transactions via `remove_entry_and_descendants` → `update_stat_for_remove_tx`, it correctly decrements `self.total_tx_size`/`self.total_tx_cycles`. However, lines 218–219 then unconditionally overwrite those fields with the stale pre-eviction values, inflating both counters by the aggregate size/cycles of every evicted entry. The inflated `total_tx_size` causes `limit_size` to expel legitimate pending transactions from the pool.

## Finding Description
`add_entry` (lines 200–221) executes in this order:

1. **Line 210–211**: `updated_stat_for_add_tx` is called on `&self` (read-only), capturing `self.total_tx_size + entry.size` into the local `total_tx_size` variable.
2. **Line 213**: `check_and_record_ancestors` is called. When `ancestors_count > max_ancestors_count` but `ancestors_count − cell_ref_parents.len() ≤ max_ancestors_count` (lines 603–625), it calls `remove_entry_and_descendants` for each cell-ref-parent, which internally calls `update_stat_for_remove_tx` (lines 733–758), **correctly** decrementing `self.total_tx_size` and `self.total_tx_cycles` in place.
3. **Lines 218–219**: `self.total_tx_size = total_tx_size` and `self.total_tx_cycles = total_tx_cycles` overwrite the now-correct decremented values with the stale pre-eviction snapshot.

After the overwrite, `self.total_tx_size` equals `old_total + entry.size` instead of the correct `old_total − sum(evicted.size) + entry.size`. The inflation is exactly `sum(evicted.size)`.

`limit_size` in `pool.rs` (lines 292–329) loops while `self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting legitimate pending transactions to compensate for the phantom inflation. Each eviction correctly decrements the counter via `update_stat_for_remove_tx`, so the counter eventually normalizes — but only after honest transactions have been expelled.

The existing `recompute_total_stat` fallback in `update_stat_for_remove_tx` (line 743) only fires on underflow, not on the overwrite path, so it provides no protection here.

## Impact Explanation
Every successful trigger evicts legitimate pending transactions worth `sum(evicted.size)` bytes from the pool. Repeated triggering continuously drains the pool of honest users' transactions, constituting a **transaction-pool DoS**. Honest users' transactions are expelled and must be resubmitted at cost. The `tx_pool_info` RPC also returns incorrect `total_tx_size`/`total_tx_cycles`, corrupting fee-estimation and monitoring tooling. This matches: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation
The trigger requires only standard `send_transaction` RPC access. The attacker must construct a transaction whose ancestor set exceeds `max_ancestors_count` (default 25) but where removing cell-ref-parents brings it within the limit. This is a well-defined, reproducible construction: submit a chain of ≥25 transactions where one ancestor's output is simultaneously spent as an input by the new transaction and referenced as a cell-dep by another in-pool transaction. No privileged access, majority hashpower, or social engineering is required. The attack is repeatable with fresh transaction sets.

## Recommendation
Move the stat-update call to **after** all evictions have been applied, so `self.total_tx_size` already reflects evictions before the new entry's contribution is added:

```rust
// In add_entry, remove the pre-computation at lines 210-211.
// After check_and_record_ancestors returns:
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Now compute stats against the already-eviction-adjusted self.total_tx_size:
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

This ensures the final counters equal `(post-eviction total) + entry.size`, which is correct.

## Proof of Concept
1. Mine enough blocks so a coinbase output is spendable.
2. Submit `T1` spending a confirmed output (produces outputs at indices 0 and 1).
3. Submit `T_dep` referencing `T1:0` as a **cell dep** (not an input).
4. Submit a chain `T2 → T3 → … → T25`, each spending the previous transaction's output.
5. Submit `T26` with two inputs: `T25:0` (chain tip) and `T1:0` (also referenced as cell dep by `T_dep`).
   - `ancestors_count = 26` (T1–T25 + T_dep) > `max_ancestors_count = 25`.
   - `cell_ref_parents = {T_dep}` (T_dep uses T1:0 as cell dep; T26 spends T1:0).
   - `26 − 1 = 25 ≤ 25` → eviction branch fires; T_dep is removed via `remove_entry_and_descendants`.
   - `update_stat_for_remove_tx(T_dep.size, T_dep.cycles)` correctly decrements `self.total_tx_size`.
   - Lines 218–219 then overwrite `self.total_tx_size` with the pre-eviction snapshot, inflating by `T_dep.size`.
6. `limit_size` observes the inflated `total_tx_size` and evicts a legitimate pending transaction.
7. Repeat steps 1–6 with fresh transactions to continuously drain the pool.