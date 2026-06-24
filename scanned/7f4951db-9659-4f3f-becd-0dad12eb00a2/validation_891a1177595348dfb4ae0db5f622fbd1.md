Audit Report

## Title
`add_entry()` Overwrites Post-Eviction Pool Size/Cycle Totals with Stale Pre-Eviction Snapshot — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry()`, `total_tx_size` and `total_tx_cycles` are snapshotted via `updated_stat_for_add_tx()` before ancestor eviction occurs. If `check_and_record_ancestors()` triggers eviction, `update_stat_for_remove_tx()` correctly decrements the live fields in place — but lines 218–219 then unconditionally overwrite those corrected fields with the stale pre-eviction snapshot. Each eviction event permanently inflates the pool's accounting totals by the size/cycles of every evicted transaction, causing the pool to report a larger occupancy than reality and eventually reject all incoming transactions with `Reject::Full`.

## Finding Description

The exact sequence in `add_entry()` (lines 200–221):

```
L210-211: (total_tx_size, total_tx_cycles) = self.updated_stat_for_add_tx(entry.size, entry.cycles)?
          // Snapshot: total_tx_size = self.total_tx_size + entry.size  (pre-eviction)

L213:     evicts = self.check_and_record_ancestors(&mut entry)?
          // When ancestors_count > max_ancestors_count AND
          //   ancestors_count - cell_ref_parents.len() <= max_ancestors_count:
          //   → calls remove_entry_and_descendants()
          //   → calls remove_entry()
          //   → calls update_stat_for_remove_tx()
          //   → self.total_tx_size -= evicted_size  (correct, in-place)
          //   → self.total_tx_cycles -= evicted_cycles (correct, in-place)

L218-219: self.total_tx_size  = total_tx_size;   // OVERWRITES correct post-eviction value
          self.total_tx_cycles = total_tx_cycles; // OVERWRITES correct post-eviction value
```

`updated_stat_for_add_tx` takes `&self` (immutable) and returns a computed tuple — it does not modify `self`. [1](#0-0) 

`check_and_record_ancestors` calls `remove_entry_and_descendants` when the eviction condition is met. [2](#0-1) 

`remove_entry` calls `update_stat_for_remove_tx`, which modifies `self.total_tx_size` and `self.total_tx_cycles` in place. [3](#0-2) 

`update_stat_for_remove_tx` performs the correct in-place subtraction. [4](#0-3) 

The final write-back on lines 218–219 uses the stale snapshot, discarding the correct post-eviction values. [5](#0-4) 

Net inflation per eviction event: `sum(evicted_tx.size)` bytes and `sum(evicted_tx.cycles)` cycles are permanently added to the reported totals. The inflated totals are exposed directly via RPC and used for pool-full enforcement. [6](#0-5) 

## Impact Explanation

**High** — matches "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."

`total_tx_size` is the primary gate for pool admission. After each eviction-triggering `add_entry` call, the pool's recorded size is permanently inflated by the evicted entries' sizes. Over repeated eviction events, `total_tx_size` diverges further above the true value. Eventually the pool rejects all new transactions even though actual occupancy is well below the configured limit. An attacker who can craft the eviction-triggering scenario can render the mempool of any targeted node permanently unable to accept new transactions, effectively denying service to all users of that node. If applied at scale across multiple nodes, this constitutes network-wide mempool congestion achievable at low cost.

## Likelihood Explanation

**Medium.** The eviction branch in `check_and_record_ancestors` requires two conditions to hold simultaneously:
1. The new transaction's ancestor count exceeds `max_ancestors_count`.
2. Enough of those ancestors are `cell_ref_parents` (transactions using a cell as `cell_dep` that the new transaction spends as an input) that evicting them brings the count within limits.

An unprivileged `send_transaction` RPC caller can deliberately engineer this: first submit a set of transactions that reference a specific live cell as a `cell_dep`, then submit a long-chain transaction that spends that cell as an input. This is repeatable, requires no special privileges, and can be scripted.

## Recommendation

Compute the new totals **after** evictions complete, not before. Remove the pre-eviction snapshot pattern and instead apply the new entry's contribution directly after `check_and_record_ancestors` returns:

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
    // Validate limits (overflow check) before any mutation — no state change yet
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    // Apply new entry's contribution AFTER evictions have already updated totals
    self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
    self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
    Ok((true, evicts))
}
```

## Proof of Concept

**Setup:** `max_ancestors_count = 25`, `max_tx_pool_size = 10_000` bytes. Pool holds 20 transactions (total size = 3,000 bytes), some of which use `cell_X` as a `cell_dep`.

**Steps:**

1. Submit `tx_A` (size = 500 bytes) that spends `cell_X` as an input and has 24 in-pool ancestors. `ancestors_count = 25 > max_ancestors_count`, but `cell_ref_parents` can be evicted to satisfy the limit — eviction branch is entered.

2. Three `cell_ref_parent` transactions (total size = 1,500 bytes) are evicted via `remove_entry_and_descendants`. `update_stat_for_remove_tx` correctly sets `self.total_tx_size = 3,000 − 1,500 = 1,500`.

3. Lines 218–219 overwrite: `self.total_tx_size = 3,000 + 500 = 3,500` (stale snapshot). Correct value: `1,500 + 500 = 2,000`. Ghost inflation: **+1,500 bytes**.

4. Repeat step 1 with fresh `cell_ref_parents`. After ~5 iterations, `total_tx_size` exceeds `max_tx_pool_size = 10,000` even though the real pool holds far fewer bytes. All subsequent `send_transaction` calls return `Reject::Full`.

**Verification:** A unit test can assert `pool_map.total_tx_size == pool_map.recompute_total_stat().unwrap().0` after each `add_entry` call that triggers eviction. The invariant will fail, confirming the inflation.

### Citations

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

**File:** tx-pool/src/component/pool_map.rs (L733-758)
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
            _ => {
                if let Some((total_tx_size, total_tx_cycles)) = self.recompute_total_stat() {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, recomputed size {} cycles {}",
                        tx_size, cycles, total_tx_size, total_tx_cycles
                    );
                    self.total_tx_size = total_tx_size;
                    self.total_tx_cycles = total_tx_cycles;
                } else {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, and recomputing overflowed",
                        tx_size, cycles
                    );
                }
            }
        }
    }
```

**File:** tx-pool/src/service.rs (L1086-1096)
```rust
            pending_size: tx_pool.pool_map.pending_size(),
            proposed_size: tx_pool.pool_map.proposed_size(),
            orphan_size: orphan.len(),
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
            min_fee_rate: self.tx_pool_config.min_fee_rate,
            min_rbf_rate: self.tx_pool_config.min_rbf_rate,
            last_txs_updated_at: tx_pool.pool_map.get_max_update_time(),
            tx_size_limit: TRANSACTION_SIZE_LIMIT,
            max_tx_pool_size: self.tx_pool_config.max_tx_pool_size as u64,
            verify_queue_size: verify_queue.len(),
```
