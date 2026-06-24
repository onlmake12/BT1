Audit Report

## Title
`add_entry()` Overwrites Post-Eviction Pool Size/Cycle Totals with Stale Pre-Eviction Snapshot — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry()`, `updated_stat_for_add_tx` is a `&self` method that returns a pre-eviction snapshot of `(total_tx_size + entry.size, total_tx_cycles + entry.cycles)` without mutating `self`. The subsequent call to `check_and_record_ancestors` may trigger `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in place. Lines 218–219 then unconditionally overwrite those correct post-eviction values with the stale pre-eviction snapshot, permanently inflating the pool's accounting totals by the sizes and cycles of every evicted transaction.

## Finding Description
The exact code sequence is confirmed in the repository:

- **Lines 210–211** ( [1](#0-0) ): `updated_stat_for_add_tx` is called as `&self` and returns `(self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles)` — a local snapshot, no mutation of `self`.

- **Lines 711–729** ( [2](#0-1) ): `updated_stat_for_add_tx` only checks for integer overflow via `checked_add`; it does not mutate `self` and does not check against `max_tx_pool_size`.

- **Line 213** ( [3](#0-2) ): `check_and_record_ancestors` is called. At lines 603–625 ( [4](#0-3) ), when `ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count`, it calls `remove_entry_and_descendants`.

- **Lines 252–264** ( [5](#0-4) ): `remove_entry_and_descendants` calls `remove_entry` for each evicted entry.

- **Lines 235–249** ( [6](#0-5) ): `remove_entry` calls `update_stat_for_remove_tx`, which at lines 738–740 ( [7](#0-6) ) directly decrements `self.total_tx_size` and `self.total_tx_cycles` — the correct post-eviction values.

- **Lines 218–219** ( [8](#0-7) ): These unconditionally overwrite `self.total_tx_size` and `self.total_tx_cycles` with the stale pre-eviction snapshot, discarding all correct decrements from evictions. Ghost inflation = sum of all evicted transaction sizes/cycles.

The `recompute_total_stat` fallback at lines 698–708 ( [9](#0-8) ) is only triggered on underflow inside `update_stat_for_remove_tx`; it is never triggered by inflation, so the ghost inflation persists indefinitely.

## Impact Explanation
The inflated `total_tx_size` and `total_tx_cycles` are directly read for RPC reporting at lines 1089–1090 of `service.rs` ( [10](#0-9) ), making pool statistics permanently incorrect after each eviction-triggering `add_entry` call. Additionally, `max_tx_pool_size` is checked in `pool.rs` and `service.rs` (confirmed by grep), and those checks consume `pool_map.total_tx_size`. Once cumulative ghost inflation drives `total_tx_size` above `max_tx_pool_size`, all subsequent `send_transaction` calls are rejected while actual pool occupancy remains well below the limit — a local mempool denial-of-service. This matches **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, as a node whose mempool permanently rejects all incoming transactions cannot participate in transaction propagation.

## Likelihood Explanation
The eviction branch requires a new transaction whose ancestor count exceeds `max_ancestors_count` and whose ancestors include `cell_ref_parents` (transactions using a cell as `cell_dep` that the new transaction consumes as an input). An unprivileged external caller via `send_transaction` RPC can deliberately construct this: first submit transactions referencing a specific live cell as `cell_dep`, then submit a long-chain transaction spending that cell as an input. This is repeatable, requires no special privileges, and can be scripted to trigger the inflation multiple times until the pool is permanently wedged.

## Recommendation
Remove the pre-eviction snapshot pattern. Call `updated_stat_for_add_tx` only for its overflow validation (discarding the return value), then apply the new entry's contribution to `self.total_tx_size` and `self.total_tx_cycles` directly after evictions have already updated them:

```rust
// Validate limits before any mutation (no state change yet)
self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Apply new entry AFTER evictions have already updated totals
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

## Proof of Concept
1. Set `max_ancestors_count = 25`. Submit transactions `T1..T20` (total size = 3,000 bytes), some referencing `cell_X` as `cell_dep`.
2. Submit `tx_A` (size = 500 bytes) spending `cell_X` as input with 24 in-pool ancestors → eviction branch at line 603 is entered.
3. Three `cell_ref_parent` transactions (total size = 1,500 bytes) are evicted. `update_stat_for_remove_tx` correctly sets `self.total_tx_size = 1,500`.
4. Lines 218–219 overwrite: `self.total_tx_size = 3,000 + 500 = 3,500`. Correct value = `1,500 + 500 = 2,000`. Ghost inflation = 1,500 bytes.
5. Repeat with fresh `cell_ref_parent` setups. After several iterations, `total_tx_size` exceeds `max_tx_pool_size` while actual pool occupancy remains low.

A unit test can be written directly against `PoolMap`: construct the eviction scenario, call `add_entry`, then assert `pool_map.total_tx_size == recompute_total_stat().0 + entry.size` using `recompute_total_stat()` as the ground-truth comparator.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-211)
```rust
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
```

**File:** tx-pool/src/component/pool_map.rs (L213-213)
```rust
        evicts = self.check_and_record_ancestors(&mut entry)?;
```

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L235-249)
```rust
    pub(crate) fn remove_entry(&mut self, id: &ProposalShortId) -> Option<TxEntry> {
        self.entries.remove_by_id(id).map(|entry| {
            debug!(
                "remove entry {} from status: {:?}",
                entry.inner.transaction().hash(),
                entry.status
            );
            self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
            self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
            self.remove_entry_edges(&entry.inner);
            self.remove_entry_links(id);
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
            entry.inner
        })
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

**File:** tx-pool/src/component/pool_map.rs (L698-708)
```rust
    fn recompute_total_stat(&self) -> Option<(usize, Cycle)> {
        self.entries.iter().try_fold(
            (0usize, 0 as Cycle),
            |(total_size, total_cycles), (_, entry)| {
                Some((
                    total_size.checked_add(entry.inner.size)?,
                    total_cycles.checked_add(entry.inner.cycles)?,
                ))
            },
        )
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

**File:** tx-pool/src/component/pool_map.rs (L738-740)
```rust
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/service.rs (L1089-1090)
```rust
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
```
