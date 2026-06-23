### Title
`total_tx_size` / `total_tx_cycles` Inflated When Evictions Occur During `add_entry` - (File: `tx-pool/src/component/pool_map.rs`)

### Summary

In `PoolMap::add_entry`, the new running totals (`total_tx_size`, `total_tx_cycles`) are computed **before** ancestor-eviction side-effects occur, stored in local variables, and then unconditionally written back **after** evictions have already correctly decremented those same fields. This overwrites the correct post-eviction values with stale pre-eviction values, permanently inflating the pool's reported size and cycle count by the sum of all evicted transactions.

### Finding Description

`add_entry` in `tx-pool/src/component/pool_map.rs` follows this sequence:

1. **Line 210–211**: `updated_stat_for_add_tx` computes `total_tx_size = self.total_tx_size + entry.size` and `total_tx_cycles = self.total_tx_cycles + entry.cycles` and stores them in **local variables**. [1](#0-0) 

2. **Line 213**: `check_and_record_ancestors` is called. When the ancestor count exceeds `max_ancestors_count` but can be reduced by evicting `cell_ref_parents`, it calls `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which **correctly decrements** `self.total_tx_size` and `self.total_tx_cycles` for each evicted transaction. [2](#0-1) [3](#0-2) 

3. **Lines 218–219**: The stale local variables (computed before evictions) are written back, **overwriting** the correctly-decremented `self.total_tx_size` and `self.total_tx_cycles`. [4](#0-3) 

After this sequence, `total_tx_size` equals `old_total + new_entry_size` instead of the correct `old_total - evicted_sizes + new_entry_size`. The pool's accounting is permanently inflated by `sum(evicted_tx.size)` per triggering insertion.

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size()` to decide when to evict transactions from the pool: [5](#0-4) 

An inflated `total_tx_size` causes `limit_size()` to evict additional legitimate transactions even though the pool has real capacity remaining. Additionally, `updated_stat_for_add_tx` uses `total_tx_size` to reject new submissions with `Reject::Full`: [6](#0-5) 

The inflation accumulates with each triggering insertion, progressively shrinking the effective pool capacity below `max_tx_pool_size`. This causes legitimate transactions to be prematurely evicted or rejected, degrading node throughput and mempool utility permanently until the node restarts.

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is triggered when a submitted transaction has a cell dep whose output is already consumed by a pool transaction (`cell_ref_parents`), and the ancestor count exceeds `max_ancestors_count`. An unprivileged tx-pool submitter (via RPC `send_transaction` or P2P relay) can craft transactions that deliberately reference cell deps already consumed by pool transactions to repeatedly trigger this path. No special privilege is required. [7](#0-6) 

### Recommendation

Compute the new totals **after** evictions complete, not before. Replace the pre-eviction snapshot approach with an additive update applied to the already-correct `self.total_tx_size` after `check_and_record_ancestors` returns:

```rust
// After check_and_record_ancestors, self.total_tx_size is already
// correctly decremented for evictions. Just add the new entry.
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Remove the pre-eviction call to `updated_stat_for_add_tx` (or move the overflow check to after evictions using the updated `self.total_tx_size`).

### Proof of Concept

1. Fill the pool with a chain of transactions up to `max_ancestors_count - 1` ancestors, where the last transaction in the chain uses a cell dep output (`cell_ref_parent`).
2. Submit a new transaction that also references that same cell dep output as an ancestor, pushing `ancestors_count` above `max_ancestors_count`.
3. `check_and_record_ancestors` evicts the `cell_ref_parent` transaction (and its descendants) via `remove_entry_and_descendants`, correctly decrementing `self.total_tx_size`.
4. `add_entry` then overwrites `self.total_tx_size` with the stale pre-eviction value.
5. Query `tx_pool_info` via RPC: `total_tx_size` is now larger than the sum of actual pool entries' sizes.
6. Repeat: each triggering insertion inflates `total_tx_size` further, causing `limit_size()` to evict legitimate transactions and `updated_stat_for_add_tx` to falsely reject new submissions as `Reject::Full`. [8](#0-7)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L200-221)
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
        Ok((true, evicts))
    }
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
