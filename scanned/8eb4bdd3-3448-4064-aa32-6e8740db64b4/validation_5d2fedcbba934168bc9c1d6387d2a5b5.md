The code confirms the claim. Let me verify the `update_stat_for_remove_tx` and `remove_entry_and_descendants` chain to complete the analysis.

The code fully confirms the claim. All three steps of the bug are verified in the actual source:

- **L210-211**: `updated_stat_for_add_tx` snapshots `self.total_tx_size + entry.size` into locals before any eviction.
- **L213**: `check_and_record_ancestors` → `remove_entry_and_descendants` (L263) → `remove_entry` (L247) → `update_stat_for_remove_tx` (L738-740) correctly decrements `self.total_tx_size`/`self.total_tx_cycles` in place.
- **L218-219**: The stale pre-eviction locals are unconditionally written back, overwriting the correct decremented values.

The eviction path at L603-625 is reachable by any unprivileged submitter. The `limit_size` loop at L298 directly reads the inflated `self.pool_map.total_tx_size`, causing cascading unnecessary evictions of valid transactions.

---

Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Permanently Inflated When Ancestor-Limit Eviction Occurs During `add_entry` - (File: tx-pool/src/component/pool_map.rs)

## Summary
In `PoolMap::add_entry`, the updated `total_tx_size` and `total_tx_cycles` are computed from `self.total_tx_size` before `check_and_record_ancestors` runs. When that function evicts existing transactions via `remove_entry_and_descendants` → `update_stat_for_remove_tx`, it correctly decrements `self.total_tx_size` in place. However, the final assignment at L218-219 overwrites those correct decrements with the stale pre-eviction snapshot, permanently inflating both counters by the aggregate size/cycles of every evicted transaction. The inflated `total_tx_size` is then used by `limit_size` to drive further unnecessary evictions of valid, fee-paying transactions.

## Finding Description
`add_entry` (L200-221) follows this sequence:

1. **L210-211**: `updated_stat_for_add_tx` reads `self.total_tx_size` and returns `self.total_tx_size + entry.size` as a local variable — before any eviction has occurred.
2. **L213**: `check_and_record_ancestors` is called. When `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (L603), it enters the eviction loop (L615-625), calling `remove_entry_and_descendants` (L618) for each candidate. `remove_entry_and_descendants` calls `remove_entry` (L263), which calls `update_stat_for_remove_tx` (L247), which correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in place (L738-740).
3. **L218-219**: `self.total_tx_size = total_tx_size` and `self.total_tx_cycles = total_tx_cycles` unconditionally overwrite the correctly-decremented live fields with the stale pre-eviction snapshot.

After one such event where evicted transactions have aggregate size `S_evicted`, `self.total_tx_size` is inflated by exactly `S_evicted`. The inflation is permanent and cumulative across repeated events. No existing guard re-validates or recomputes the totals after `add_entry` returns.

## Impact Explanation
`total_tx_size` is the sole gate in `limit_size` (pool.rs L298): `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`. An inflated counter causes this loop to evict additional valid, fee-paying transactions that should remain in the pool. An adversary who repeatedly engineers the eviction condition can continuously flush competing transactions from the pool at the cost of their own transaction fees, degrading pool quality and causing CKB network congestion. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
The trigger requires submitting a transaction that (a) references a cell dep whose out-point is already tracked in `edges.deps` by existing pool transactions, and (b) has enough in-pool ancestors to push the count above `max_ancestors_count` before the cell-dep-referencing parents are removed. Both conditions are achievable by an unprivileged P2P relayer or RPC caller with no special keys or hash-power. An adversary can deliberately pre-populate the pool with a chain of transactions sharing a cell dep, then submit the triggering transaction. The attack is repeatable: each round inflates the counter further, compounding the effect.

## Recommendation
Move `updated_stat_for_add_tx` to after `check_and_record_ancestors` completes, so it reads the already-decremented `self.total_tx_size`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute AFTER evictions have already decremented self.total_tx_size/cycles
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, apply the delta directly (`self.total_tx_size += entry.size`) after evictions rather than using a pre-computed snapshot.

## Proof of Concept
1. Pool starts empty; `total_tx_size = 0`, `max_ancestors_count = N`.
2. Submit transactions `T1…TN` that all reference the same cell dep `D` and form a chain of `N` ancestors. Each has size 100. `total_tx_size = N * 100`.
3. Submit transaction `X` (size 50) that also references cell dep `D` as a cell dep.
4. In `add_entry` for `X`:
   - L210-211: `total_tx_size_local = N*100 + 50`.
   - L213: `check_and_record_ancestors` finds `ancestors_count = N+1 > N`; `cell_ref_parents` contains `T1`; evicts `T1` (size 100). `self.total_tx_size` is correctly decremented to `(N-1)*100`.
   - L218: `self.total_tx_size = N*100 + 50` (stale). Correct value: `(N-1)*100 + 50`.
5. `total_tx_size` is now inflated by 100 (size of evicted `T1`).
6. If `max_tx_pool_size = (N-1)*100 + 50`, `limit_size` now sees `N*100 + 50 > max_tx_pool_size` and evicts additional valid transactions unnecessarily.
7. Repeat step 3 with a new `X'` to inflate by another 100 each round.