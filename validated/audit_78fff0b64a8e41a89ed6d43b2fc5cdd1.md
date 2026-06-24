Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Permanently Inflated by Stale Overwrite After Cell-Dep Eviction in `add_entry()` — (File: tx-pool/src/component/pool_map.rs)

## Summary
In `PoolMap::add_entry()`, pool-wide size and cycle totals are pre-computed before `check_and_record_ancestors()` runs. When that function evicts cell-dep-referencing transactions, `update_stat_for_remove_tx()` correctly decrements `self.total_tx_size` and `self.total_tx_cycles`. However, the pre-computed stale values are then unconditionally written back at lines 218–219, permanently overwriting the correct decremented values. The result is that `total_tx_size` and `total_tx_cycles` are inflated by the aggregate size/cycles of every evicted transaction, causing the pool to falsely believe it is fuller than it actually is and reject or evict legitimate transactions.

## Finding Description
In `add_entry()` (`pool_map.rs` L200–221):

`updated_stat_for_add_tx()` is called at L210–211 before any evictions occur. It takes `&self` (immutable borrow) and returns a plain integer snapshot of `self.total_tx_size + entry.size`. [1](#0-0) 

At L213, `check_and_record_ancestors()` may trigger the eviction path at L603–625 when `ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count` but `ancestors_count > self.max_ancestors_count`. Each call to `remove_entry_and_descendants()` at L618 internally calls `update_stat_for_remove_tx()`, which directly mutates `self.total_tx_size` and `self.total_tx_cycles` downward. [2](#0-1) [3](#0-2) 

At L218–219, the stale pre-eviction snapshot is unconditionally written back, discarding all decrements performed by `update_stat_for_remove_tx()`: [4](#0-3) 

Let `T = self.total_tx_size` before `add_entry()`. After the call:
- Correct value: `T - S_evict + entry.size`
- Actual value written: `T + entry.size`
- Permanent inflation: `S_evict` (aggregate size of all evicted transactions)

No existing guard corrects this. The `recompute_total_stat()` fallback in `update_stat_for_remove_tx()` is only triggered on underflow (L743), which does not occur here since the stale overwrite happens after the decrement path completes.

## Impact Explanation
The inflated counter has two concrete downstream effects:

**1. False pool-full rejections / unnecessary evictions.** `limit_size()` loops while `self.pool_map.total_tx_size > self.config.max_tx_pool_size` and evicts legitimate transactions to satisfy the false limit. [5](#0-4) 

**2. Incorrect admission gating.** `updated_stat_for_add_tx()` uses the inflated `self.total_tx_size` as the base for subsequent overflow/limit checks, causing honest transactions to be rejected with `Reject::Full` even when real pool occupancy is well below `max_tx_pool_size`. [6](#0-5) 

**3. Misleading RPC state.** `TxPoolInfo.total_tx_size` is read directly from `pool_map.total_tx_size`, so `get_pool_info` returns incorrect data to all callers. [7](#0-6) 

This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** An attacker can cheaply and repeatedly inflate the pool counter on any node, causing it to reject or evict legitimate transactions. Applied across multiple nodes, this degrades network-wide transaction propagation.

## Likelihood Explanation
The trigger requires no privileged access, key material, or majority hash power. Any unprivileged submitter can submit ~2000 low-fee transactions each referencing the same cell dep, then submit one transaction spending that cell dep. This is exactly the scenario exercised by the existing integration test `TxPoolLimitAncestorCount`, confirming the path is reachable in production. The cost is ~2001 transaction fees. The inflation is permanent until the node restarts or `recompute_total_stat()` is triggered by an underflow via a separate removal path. The attack is repeatable.

## Recommendation
Move `updated_stat_for_add_tx()` to **after** `check_and_record_ancestors()` completes, so it uses the already-decremented `self.total_tx_size` as its base:

```rust
pub(crate) fn add_entry(
    &mut self,
    mut entry: TxEntry,
    status: Status,
) -> Result<(bool, HashSet<TxEntry>), Reject> {
    let tx_short_id = entry.proposal_short_id();
    let mut evicts = Default::default();
    if self.entries.get_by_id(&tx_short_id).is_some() {
        return Ok((false, evicts));
    }
    // Evictions must happen first so their decrements are reflected in the base
    evicts = self.check_and_record_ancestors(&mut entry)?;
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    self.total_tx_size = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;
    Ok((true, evicts))
}
```

## Proof of Concept
Using the existing `TxPoolLimitAncestorCount` integration test as a template:

1. Record `pool_info.total_tx_size = T` after submitting 2000 cell-dep-referencing transactions.
2. Submit a transaction spending `tx_a`'s output. `check_and_record_ancestors()` evicts 1002 transactions (total evicted size `S_evict`). `update_stat_for_remove_tx()` is called 1002 times, correctly computing `self.total_tx_size = T - S_evict`.
3. Lines 218–219 overwrite: `self.total_tx_size = T + entry.size`.
4. Call `get_pool_info` RPC. Observe `total_tx_size ≈ T + entry.size` instead of the correct `T - S_evict + entry.size`. The difference is `S_evict` (≈ 1002 × avg_tx_size).
5. Submit additional transactions that would be accepted under real occupancy. Observe `Reject::Full` responses and/or observe `limit_size()` evicting honest transactions unnecessarily.

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

**File:** tx-pool/src/component/pool_map.rs (L716-721)
```rust
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_size {} overflows by add {}",
                self.total_tx_size, tx_size
            ))
        })?;
```

**File:** tx-pool/src/component/pool_map.rs (L733-741)
```rust
    fn update_stat_for_remove_tx(&mut self, tx_size: usize, cycles: Cycle) {
        match (
            self.total_tx_size.checked_sub(tx_size),
            self.total_tx_cycles.checked_sub(cycles),
        ) {
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
            }
```

**File:** tx-pool/src/pool.rs (L298-307)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
```

**File:** tx-pool/src/service.rs (L1089-1089)
```rust
            total_tx_size: tx_pool.pool_map.total_tx_size,
```
