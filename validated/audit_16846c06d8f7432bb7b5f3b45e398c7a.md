### Title
`add_entry()` Overwrites Eviction-Adjusted Pool Size/Cycle Totals with Stale Pre-Eviction Values — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry()`, `total_tx_size` and `total_tx_cycles` are computed **before** ancestor-eviction occurs, then unconditionally written back **after** eviction, silently discarding the correct subtractions performed by `update_stat_for_remove_tx()` during eviction. This permanently inflates the pool's accounting totals, causing premature "pool full" rejections of legitimate transactions and incorrect RPC-reported pool statistics.

---

### Finding Description

`add_entry()` in `tx-pool/src/component/pool_map.rs` follows this sequence:

```
Line 210-211: (total_tx_size, total_tx_cycles) = updated_stat_for_add_tx(entry.size, entry.cycles)
              // Snapshot: new_total = old_total + entry.size  (stale, pre-eviction)

Line 213:     evicts = self.check_and_record_ancestors(&mut entry)?
              // May call remove_entry_and_descendants → remove_entry → update_stat_for_remove_tx
              // This correctly sets self.total_tx_size -= evicted_size
              //                    self.total_tx_cycles -= evicted_cycles

Line 218-219: self.total_tx_size  = total_tx_size;   // OVERWRITES the correct post-eviction value
              self.total_tx_cycles = total_tx_cycles; // OVERWRITES the correct post-eviction value
``` [1](#0-0) 

`updated_stat_for_add_tx` captures the pre-eviction totals: [2](#0-1) 

`check_and_record_ancestors` calls `remove_entry_and_descendants` when `ancestors_count > self.max_ancestors_count` and `cell_ref_parents` can be evicted to bring the count within limits: [3](#0-2) 

`remove_entry` correctly calls `update_stat_for_remove_tx`, which modifies `self.total_tx_size` and `self.total_tx_cycles` in place: [4](#0-3) [5](#0-4) 

But the final write-back on lines 218–219 uses the stale snapshot computed before eviction, overwriting the correct post-eviction values. The net result is that `total_tx_size` and `total_tx_cycles` are inflated by the sizes and cycles of every evicted transaction.

These inflated totals are then used directly for pool-full enforcement and RPC reporting: [6](#0-5) 

---

### Impact Explanation

**Impact: High**

`total_tx_size` is the primary gate for pool admission. `updated_stat_for_add_tx` checks it against the configured `max_tx_pool_size` limit: [7](#0-6) 

After each eviction-triggering `add_entry` call, the pool's recorded size is permanently inflated by the evicted entries' sizes. Over repeated eviction events, the pool's reported `total_tx_size` diverges further and further above the true value. Eventually, the pool rejects all new transactions with `Reject::Full` even though the actual pool occupancy is well below the limit. This is a complete denial-of-service against the mempool's transaction admission path, affecting all users of the node.

---

### Likelihood Explanation

**Likelihood: Medium**

The eviction path in `check_and_record_ancestors` is triggered when:
1. A new transaction's ancestor count exceeds `max_ancestors_count`, AND
2. Some of those ancestors are `cell_ref_parents` (transactions that use a cell as a dep that the new transaction consumes as an input).

An unprivileged RPC caller (`send_transaction`) can deliberately craft this scenario: first submit a set of transactions that reference a specific live cell as a `cell_dep`, then submit a long-chain transaction that spends that cell as an input. This is a realistic, repeatable, externally-reachable trigger requiring no special privileges.

---

### Recommendation

Compute the new totals **after** evictions complete, not before. Replace the pre-eviction snapshot pattern with a post-eviction calculation:

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
    // Validate limits before any mutation (no state change yet)
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    // Apply the new entry's contribution AFTER evictions have already
    // updated self.total_tx_size / self.total_tx_cycles via update_stat_for_remove_tx
    self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
    self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
    Ok((true, evicts))
}
```

---

### Proof of Concept

**Setup:**
- `max_ancestors_count = 25`, `max_tx_pool_size = 10_000` bytes
- Pool currently holds 20 transactions (total size = 3,000 bytes), some of which use `cell_X` as a `cell_dep`

**Steps:**

1. Submit `tx_A` (size = 500 bytes) that spends `cell_X` as an input and has 24 in-pool ancestors. This triggers the eviction branch in `check_and_record_ancestors` because `ancestors_count = 25 > max_ancestors_count` but `cell_ref_parents` can be evicted.

2. Suppose 3 `cell_ref_parent` transactions (total size = 1,500 bytes, total cycles = 300,000) are evicted via `remove_entry_and_descendants`. `update_stat_for_remove_tx` correctly sets `self.total_tx_size = 3,000 - 1,500 = 1,500`.

3. Lines 218–219 then overwrite: `self.total_tx_size = 3,000 + 500 = 3,500` (the stale pre-eviction snapshot). The correct value should be `1,500 + 500 = 2,000`.

4. The pool now reports `total_tx_size = 3,500` instead of `2,000`. The 1,500-byte ghost inflation persists permanently.

5. Repeat step 1 several times. After ~5 iterations, `total_tx_size` exceeds `max_tx_pool_size = 10,000` even though the real pool holds far fewer bytes. All subsequent `send_transaction` RPC calls return `Reject::Full`, denying service to all legitimate users.

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
