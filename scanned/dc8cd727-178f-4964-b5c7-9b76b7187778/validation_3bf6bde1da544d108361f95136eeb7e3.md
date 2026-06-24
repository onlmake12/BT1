Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Permanently Inflated by Cell-Dep Eviction in `add_entry()` — (File: tx-pool/src/component/pool_map.rs)

## Summary
In `PoolMap::add_entry()`, the aggregate pool statistics `total_tx_size` and `total_tx_cycles` are pre-computed into local variables before `check_and_record_ancestors()` runs. When that function evicts cell-dep-referencing transactions, `update_stat_for_remove_tx()` correctly decrements `self.total_tx_size`/`self.total_tx_cycles` in place, but the stale pre-computed locals are then unconditionally written back, silently overwriting the correct decremented values. The result is a permanent inflation of both counters by the aggregate size/cycles of every evicted transaction, causing the pool to behave as if it is fuller than it actually is.

## Finding Description
In `add_entry()` (lines 200–221 of `tx-pool/src/component/pool_map.rs`):

```
Line 210-211: total_tx_size = self.total_tx_size + entry.size  (snapshot taken)
Line 213:     check_and_record_ancestors() → remove_entry_and_descendants()
                → remove_entry() → update_stat_for_remove_tx()
                   DECREMENTS self.total_tx_size by S_evict (in-place)
Lines 218-219: self.total_tx_size = total_tx_size  ← OVERWRITES with stale snapshot
``` [1](#0-0) 

`updated_stat_for_add_tx()` (lines 711–729) is a pure read of `self.total_tx_size`; it does not mutate state, only returning the projected new value into locals. [2](#0-1) 

`update_stat_for_remove_tx()` (lines 733–758) is the authoritative in-place decrement path; it directly writes `self.total_tx_size` and `self.total_tx_cycles`. [3](#0-2) 

The eviction path inside `check_and_record_ancestors()` (lines 603–625) is triggered when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`, i.e., when a new transaction consumes a cell dep referenced by many in-pool transactions. [4](#0-3) 

`remove_entry_and_descendants()` (lines 252–264) calls `remove_entry()` for each removed transaction, which calls `update_stat_for_remove_tx()` per entry. [5](#0-4) 

After the overwrite, `self.total_tx_size` equals `T + entry.size` instead of the correct `T - S_evict + entry.size`, where `T` is the pre-eviction total and `S_evict` is the aggregate size of all evicted transactions. No existing guard corrects this after the fact; the inflation persists for the lifetime of the pool.

## Impact Explanation
The inflated `total_tx_size` has three concrete downstream effects:

1. **Unnecessary eviction of legitimate transactions.** `limit_size()` loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size` and evicts lowest-fee-rate entries. With an inflated counter, it evicts real transactions that should have remained in the pool. [6](#0-5) 

2. **False `Reject::Full` for incoming transactions.** `updated_stat_for_add_tx()` uses `self.total_tx_size` as the base; subsequent honest submissions are rejected even when actual occupancy is well below `max_tx_pool_size`. [7](#0-6) 

3. **Misleading RPC state.** `get_pool_info` reads `total_tx_size` directly from `pool_map.total_tx_size`, returning an incorrect value to all callers. [8](#0-7) 

The attack is repeatable: each trigger inflates the counter by `S_evict`. After enough repetitions the pool permanently rejects all incoming transactions with `Reject::Full` and continuously evicts its own contents, constituting a sustained denial-of-service against the mempool. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
No privileged access, key material, or majority hash power is required. Any unprivileged node peer can submit transactions. The trigger condition (many in-pool transactions sharing a cell dep, followed by a transaction that spends that cell dep's output) is demonstrated by the existing integration test `TxPoolLimitAncestorCount`. The attacker pays transaction fees for ~2001 transactions per trigger, but the inflation is permanent per trigger and the attack is repeatable, making the cost-to-impact ratio low.

## Recommendation
Move the `updated_stat_for_add_tx()` call to **after** `check_and_record_ancestors()` completes, so it uses the already-decremented `self.total_tx_size` as its base:

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
    // MOVED: compute totals AFTER evictions have decremented self.total_tx_size
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

This ensures the snapshot is taken from the post-eviction `self.total_tx_size`, so the final written value is `(T - S_evict) + entry.size` as intended.

## Proof of Concept
1. Submit 2 000 transactions each referencing `tx_a`'s output as a cell dep (as in `TxPoolLimitAncestorCount`). Record `total_tx_size = T`.
2. Submit a transaction that **spends** `tx_a`'s output. `check_and_record_ancestors()` evicts 1 002 cell-dep-referencing transactions with aggregate size `S_evict`.
3. `update_stat_for_remove_tx()` is called 1 002 times, correctly setting `self.total_tx_size = T - S_evict`.
4. Lines 218–219 overwrite: `self.total_tx_size = T + entry.size`.
5. Pool now reports `total_tx_size ≈ T + entry.size` instead of the correct `T - S_evict + entry.size`.
6. `limit_size()` immediately evicts additional honest transactions; subsequent submissions receive `Reject::Full`.
7. Repeat steps 1–6 to accumulate further inflation until the pool is effectively unusable.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-219)
```rust
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
        trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
        evicts = self.check_and_record_ancestors(&mut entry)?;
        self.record_entry_edges(&entry)?;
        self.insert_entry(&entry, status);
        self.record_entry_descendants(&entry);
        self.track_entry_statics(None, Some(status));
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L252-264)
```rust
    pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
        let mut removed_ids = vec![id.to_owned()];
        removed_ids.extend(self.calc_descendants(id));

        // update links state for remove, so that we won't update_descendants_index_key in remove_entry
        for id in &removed_ids {
            self.remove_entry_links(id);
        }

        removed_ids
            .iter()
            .filter_map(|id| self.remove_entry(id))
            .collect()
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

**File:** tx-pool/src/component/pool_map.rs (L711-729)
```rust
    fn updated_stat_for_add_tx(
        &self,
        tx_size: usize,
        cycles: Cycle,
    ) -> Result<(usize, Cycle), Reject> {
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_size {} overflows by add {}",
                self.total_tx_size, tx_size
            ))
        })?;
        let total_tx_cycles = self.total_tx_cycles.checked_add(cycles).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_cycles {} overflows by add {}",
                self.total_tx_cycles, cycles
            ))
        })?;
        Ok((total_tx_size, total_tx_cycles))
    }
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

**File:** tx-pool/src/pool.rs (L298-327)
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
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
```

**File:** tx-pool/src/service.rs (L1089-1090)
```rust
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
```
