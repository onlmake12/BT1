### Title
`total_tx_size` / `total_tx_cycles` Inflated When Evictions Occur Inside `add_entry`, Causing Spurious Pool Rejections — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::add_entry` pre-computes new `total_tx_size` / `total_tx_cycles` values **before** calling `check_and_record_ancestors`, which may internally evict existing pool entries via `remove_entry_and_descendants`. Each eviction correctly subtracts from the running totals through `update_stat_for_remove_tx`. However, `add_entry` then **overwrites** those correctly-updated totals with the stale pre-eviction values, permanently inflating both counters by the aggregate size and cycles of every evicted transaction. Because `total_tx_size` is the sole gate used by `limit_size` to decide whether to evict further transactions, an attacker who can repeatedly trigger the eviction path causes the pool to believe it is fuller than it actually is, leading to cascading spurious evictions and `Reject::Full` errors for legitimate transactions.

---

### Finding Description

In `tx-pool/src/component/pool_map.rs`, `add_entry` executes the following sequence:

```
(total_tx_size, total_tx_cycles) = updated_stat_for_add_tx(entry.size, entry.cycles)
    // ↑ snapshot computed as: self.total_tx_size + entry.size

evicts = check_and_record_ancestors(&mut entry)
    // ↑ may call remove_entry_and_descendants → remove_entry → update_stat_for_remove_tx
    //   which correctly subtracts evicted sizes from self.total_tx_size in-place

...
self.total_tx_size = total_tx_size   // ← OVERWRITES the correctly-updated value
self.total_tx_cycles = total_tx_cycles
``` [1](#0-0) 

`updated_stat_for_add_tx` captures the totals **before** any evictions: [2](#0-1) 

`check_and_record_ancestors` evicts entries when the incoming transaction has too many ancestors that are "cell-ref parents" (transactions sharing a cell dep): [3](#0-2) 

Each eviction calls `remove_entry`, which calls `update_stat_for_remove_tx` and correctly subtracts the evicted entry's size and cycles from `self.total_tx_size` / `self.total_tx_cycles` in-place: [4](#0-3) [5](#0-4) 

After `check_and_record_ancestors` returns, `self.total_tx_size` correctly reflects `S − evict_size`. But then `add_entry` unconditionally writes `total_tx_size = S + entry.size`, discarding the eviction subtraction. The net result is that `self.total_tx_size` ends up `evict_size` bytes larger than the true sum of all entries in the pool.

The inflated `total_tx_size` is then used directly in `limit_size` as the eviction trigger:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict more entries
}
``` [6](#0-5) 

And it is exposed via `TxPoolInfo` to the RPC layer: [7](#0-6) 

---

### Impact Explanation

Each attacker-triggered eviction cycle permanently inflates `total_tx_size` by the byte-size of the evicted transactions. After enough cycles:

1. `limit_size` fires even though the pool has real capacity, evicting legitimate pending transactions.
2. New `send_transaction` calls are rejected with `Reject::Full` even though the pool is not actually full.
3. Legitimate transactions are permanently excluded from the pool until a node restart (which resets the in-memory counters), or until a reorg triggers `clear()`.

This matches the external report's impact: a counter used as a resource gate is inflated by an asymmetric update, causing legitimate participants to be denied service and resources to appear permanently consumed.

---

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged `send_transaction` RPC caller. The attacker needs to:

1. Submit a set of transactions that all reference the same live cell as a `cell_dep` (valid, no special privilege required).
2. Submit a new transaction that **spends** that same cell as an input. This makes all the dep-referencing transactions become `cell_ref_parents` of the new transaction, triggering the eviction branch when the ancestor count exceeds `max_ancestors_count` (default 25).
3. Repeat. Each cycle inflates `total_tx_size` by the aggregate size of the evicted transactions.

The `send_transaction` RPC is open to any node operator or relay peer, making this reachable without any privileged access.

---

### Recommendation

Move the stat computation to **after** `check_and_record_ancestors` returns, so that the final assignment reflects the post-eviction state:

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
    // Validate capacity BEFORE evictions (unchanged)
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));

    // Compute final totals AFTER evictions have already updated self.total_tx_size
    self.total_tx_size = self.total_tx_size
        .checked_add(entry.size)
        .expect("size overflow after evictions");
    self.total_tx_cycles = self.total_tx_cycles
        .checked_add(entry.cycles)
        .expect("cycles overflow after evictions");

    Ok((true, evicts))
}
```

This ensures the eviction subtractions performed by `update_stat_for_remove_tx` are not overwritten.

---

### Proof of Concept

**Setup**: Pool with `max_ancestors_count = 25`, `max_tx_pool_size = 180 MB`.

1. Attacker submits 26 transactions `T1…T26`, each using cell `C` as a `cell_dep`. All are accepted into the pending pool. `total_tx_size` = sum of their sizes (e.g., 26 × 600 bytes = 15 600 bytes).

2. Attacker submits transaction `T_spend` that **spends** cell `C` as an input. Inside `add_entry`:
   - `updated_stat_for_add_tx` snapshots `total_tx_size_snapshot = 15 600 + size(T_spend)`.
   - `check_and_record_ancestors` finds 26 `cell_ref_parents`, evicts them via `remove_entry_and_descendants`. `update_stat_for_remove_tx` is called 26 times, reducing `self.total_tx_size` to 0.
   - `add_entry` then writes `self.total_tx_size = total_tx_size_snapshot = 15 600 + size(T_spend)`.
   - **Actual pool contents**: only `T_spend`. **Reported size**: ~15 600 bytes inflated.

3. Repeat step 1–2 `N` times. After `N` iterations, `total_tx_size` ≈ `N × 15 600` bytes while the pool actually contains only `N` copies of `T_spend` (or fewer, since they conflict).

4. Once `total_tx_size > max_tx_pool_size` (180 MB), `limit_size` begins evicting legitimate transactions and all new `send_transaction` calls return `Reject::Full`, even though the pool is nearly empty. [1](#0-0) [3](#0-2) [6](#0-5)

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

**File:** tx-pool/src/component/pool_map.rs (L235-250)
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
    }
```

**File:** tx-pool/src/component/pool_map.rs (L588-640)
```rust
    fn check_and_record_ancestors(
        &mut self,
        entry: &mut TxEntry,
    ) -> Result<HashSet<TxEntry>, Reject> {
        let tx = entry.transaction();
        let (ancestors, mut parents, cell_ref_parents) = self.get_tx_ancenstors(tx);

        let mut ancestors_count = ancestors.len() + 1;
        let mut evicted = Default::default();

        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
        }

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
        } else {
            return Err(Reject::ExceededMaximumAncestorsCount);
        }

        // some txs in `parents` are removed, now `ancestors` need to re-caculate,
        let ancestors = self
            .links
            .calc_relation_ids(parents.clone(), Relation::Parents);

        // we can assume the number now is less than `max_ancestors_count`
        assert!(ancestors.len() < self.max_ancestors_count);

        self._record_ancestors(entry, ancestors, parents);
        Ok(evicted)
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

**File:** tx-pool/src/pool.rs (L292-329)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
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
        ret
    }
```

**File:** tx-pool/src/service.rs (L1086-1097)
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
        }
```
