All critical code paths are confirmed in the actual source. Here is the validated audit report:

---

Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Permanently Inflated When Ancestor-Limit Eviction Occurs During `add_entry` - (File: tx-pool/src/component/pool_map.rs)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` snapshots `self.total_tx_size + entry.size` into locals **before** `check_and_record_ancestors` runs. When that function evicts existing transactions via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, it correctly decrements `self.total_tx_size` in place. However, the final assignment at L218-219 overwrites those correct decrements with the stale pre-eviction snapshot, permanently inflating both counters by the aggregate size/cycles of every evicted transaction.

## Finding Description
`add_entry` (L200-221) follows this sequence:

1. **L210-211**: `updated_stat_for_add_tx` is called on `&self` (immutable borrow), reading `self.total_tx_size` and returning `self.total_tx_size + entry.size` as a local — before any eviction has occurred. [1](#0-0) 

2. **L213**: `check_and_record_ancestors` is called. When `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (L603), it enters the eviction loop (L615-625), calling `remove_entry_and_descendants` (L618) for each candidate. [2](#0-1) 

3. `remove_entry_and_descendants` (L252-265) calls `remove_entry` (L263), which calls `update_stat_for_remove_tx` (L247), which correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in place at L738-740. [3](#0-2) 

4. **L218-219**: `self.total_tx_size = total_tx_size` and `self.total_tx_cycles = total_tx_cycles` unconditionally overwrite the correctly-decremented live fields with the stale pre-eviction snapshot. [4](#0-3) 

The root cause is that `updated_stat_for_add_tx` takes `&self` (L711-712) and captures a snapshot of the counters before eviction mutates them, yet its return values are written back unconditionally after eviction. [5](#0-4) 

## Impact Explanation
`total_tx_size` is used as the gate for pool size enforcement. An inflated counter causes the pool to evict additional valid, fee-paying transactions that should remain. An adversary who repeatedly engineers the eviction condition can continuously flush competing transactions from the pool at minimal cost, degrading pool quality and causing CKB network congestion. This matches: **High (10001-15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
The trigger requires submitting a transaction that (a) references a cell dep whose out-point is already tracked in `edges.deps` by existing pool transactions, and (b) has enough in-pool ancestors to push the count above `max_ancestors_count` before the cell-dep-referencing parents are removed. Both conditions are achievable by any unprivileged P2P relayer or RPC caller with no special keys or hash-power. An adversary can deliberately pre-populate the pool with a chain of transactions sharing a cell dep, then submit the triggering transaction. The attack is repeatable: each round inflates the counter further, compounding the effect.

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
3. Submit transaction `X` (size 50) that also references cell dep `D`.
4. In `add_entry` for `X`:
   - L210-211: `total_tx_size_local = N*100 + 50`.
   - L213: `check_and_record_ancestors` finds `ancestors_count = N+1 > N`; `cell_ref_parents` contains `T1`; evicts `T1` (size 100). `self.total_tx_size` is correctly decremented to `(N-1)*100`.
   - L218: `self.total_tx_size = N*100 + 50` (stale). Correct value: `(N-1)*100 + 50`.
5. `total_tx_size` is now inflated by 100 (size of evicted `T1`).
6. Repeat with new transactions to inflate by another 100 each round, driving unnecessary evictions of valid transactions from the pool.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-211)
```rust
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
```

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L603-625)
```rust
        if ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count {
            // if ancestors count exceed limitation,
            // try to evict some conflicted transactions due to ref cells

            // sort them to find out the transactions with lowest fees
            let evict_candidates: Vec<ProposalShortId> = self
                .entries
                .iter_by_evict_key()
                .filter(move |entry| cell_ref_parents.contains(&entry.id))
                .map(|x| x.id.clone())
                .collect();

            let mut iter = evict_candidates.iter();
            while ancestors_count > self.max_ancestors_count {
                if let Some(next_id) = iter.next() {
                    let removed = self.remove_entry_and_descendants(next_id);
                    ancestors_count = ancestors_count.saturating_sub(1);
                    parents.remove(next_id);
                    evicted.extend(removed);
                } else {
                    break;
                }
            }
```

**File:** tx-pool/src/component/pool_map.rs (L711-716)
```rust
    fn updated_stat_for_add_tx(
        &self,
        tx_size: usize,
        cycles: Cycle,
    ) -> Result<(usize, Cycle), Reject> {
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
```

**File:** tx-pool/src/component/pool_map.rs (L738-740)
```rust
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
```
