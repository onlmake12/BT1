Now I have enough context to analyze the analog. Let me verify the exact lines in `add_entry` and `check_and_record_ancestors` to confirm the bug.

### Title
`add_entry` Overwrites Post-Eviction `total_tx_size`/`total_tx_cycles` with Stale Pre-Eviction Values - (File: tx-pool/src/component/pool_map.rs)

### Summary
In `PoolMap::add_entry`, the new totals for `total_tx_size` and `total_tx_cycles` are computed **before** the ancestor-eviction path runs. When `check_and_record_ancestors` evicts entries (via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`), those evictions correctly decrement `self.total_tx_size` and `self.total_tx_cycles`. However, `add_entry` then unconditionally overwrites those correctly-updated fields with the stale pre-eviction computed values, permanently inflating both counters by the aggregate size and cycles of all evicted transactions.

### Finding Description
In `tx-pool/src/component/pool_map.rs`, `add_entry` follows this sequence:

```rust
// Step 1: compute new totals from CURRENT (pre-eviction) self.total_tx_size/cycles
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // line 210-211

// Step 2: may evict entries via remove_entry_and_descendants → remove_entry
//         → update_stat_for_remove_tx, which CORRECTLY decrements
//         self.total_tx_size and self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;          // line 213

// ...

// Step 3: OVERWRITES the now-correct post-eviction values with the
//         stale pre-eviction snapshot captured in Step 1
self.total_tx_size = total_tx_size;                             // line 218
self.total_tx_cycles = total_tx_cycles;                         // line 219
```

The eviction path inside `check_and_record_ancestors` is reached when a submitted transaction's ancestor count exceeds `max_ancestors_count` but the excess is entirely composed of `cell_ref_parents` (pool transactions referenced as cell deps). In that case the code evicts those cell-dep parents:

```rust
while ancestors_count > self.max_ancestors_count {
    if let Some(next_id) = iter.next() {
        let removed = self.remove_entry_and_descendants(next_id);
        // ↑ calls remove_entry → update_stat_for_remove_tx
        //   correctly subtracts evicted size/cycles from self.total_tx_size/cycles
        ...
    }
}
```

After the eviction loop, `self.total_tx_size` and `self.total_tx_cycles` correctly reflect `(original − evicted_size + 0)` and `(original − evicted_cycles + 0)`. But lines 218-219 then restore them to `(original + entry.size)` and `(original + entry.cycles)`, discarding the eviction subtractions entirely. The net effect is that both counters are inflated by exactly the aggregate size and cycles of every evicted transaction. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

### Impact Explanation
`total_tx_size` and `total_tx_cycles` are the two authoritative pool-accounting counters. Their inflation has three concrete downstream effects:

1. **Premature pool eviction / spurious `Reject::Full`**: `limit_size` loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size` and evicts entries until the condition is false. An inflated `total_tx_size` causes the loop to evict additional legitimate transactions that would otherwise fit, and `updated_stat_for_add_tx` may reject incoming transactions with `Reject::Full` even when the pool has room.

2. **Incorrect RPC reporting**: `tx_pool_info` reads `total_tx_size` and `total_tx_cycles` directly from `pool_map` and returns them to callers. Wallets, monitoring tools, and fee-estimation logic that rely on these values receive incorrect data.

3. **Cascading inflation**: Each subsequent `add_entry` call that triggers the eviction path adds another layer of inflation, so the error compounds over time without a node restart or `clear_tx_pool`. [5](#0-4) [6](#0-5) [7](#0-6) 

### Likelihood Explanation
The eviction path requires a submitted transaction to have ancestors that exceed `max_ancestors_count` where the excess ancestors are exclusively `cell_ref_parents`. This is a non-default but fully reachable condition: any RPC caller can submit a chain of transactions where later transactions reference earlier ones as cell deps, then submit a transaction that pushes the ancestor count over the limit. No privileged role, leaked key, or majority hashpower is required — only the ability to call `send_transaction` via the public JSON-RPC interface. [8](#0-7) [9](#0-8) 

### Recommendation
Move the stat computation to **after** the eviction step, so it operates on the already-updated `self.total_tx_size` and `self.total_tx_cycles`:

```rust
pub(crate) fn add_entry(
    &mut self,
    mut entry: TxEntry,
    status: Status,
) -> Result<(bool, HashSet<TxEntry>), Reject> {
    let tx_short_id = entry.proposal_short_id();
    if self.entries.get_by_id(&tx_short_id).is_some() {
        return Ok((false, Default::default()));
    }
    // Validate that adding this entry won't overflow (pre-check only)
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

    let evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));

    // Compute final totals AFTER evictions have already adjusted the counters
    self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
    self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
    Ok((true, evicts))
}
```

Alternatively, recompute the totals from scratch via `recompute_total_stat` after the eviction step, similar to the underflow-recovery path already present in `update_stat_for_remove_tx`. [10](#0-9) 

### Proof of Concept
Assume `max_ancestors_count = 25`, `max_tx_pool_size = 20 MB`, and the pool currently holds 24 transactions in a chain where `tx_A` is referenced as a cell dep by `tx_B` (both in the pool). Submit `tx_C` that:
- Spends an output of `tx_B` (making `tx_B` and `tx_A` ancestors, count = 26 > 25)
- Has `tx_A` as a cell dep (making `tx_A` a `cell_ref_parent`)

Execution path:
1. `add_entry(tx_C, Pending)` is called.
2. `updated_stat_for_add_tx` snapshots `total_tx_size_snapshot = self.total_tx_size + size(tx_C)`.
3. `check_and_record_ancestors` detects ancestor count = 26 > 25, and `cell_ref_parents = {tx_A}`. It calls `remove_entry_and_descendants(tx_A)`, which removes `tx_A` and `tx_B` and correctly decrements `self.total_tx_size` by `size(tx_A) + size(tx_B)`.
4. Lines 218-219 overwrite `self.total_tx_size` with `total_tx_size_snapshot`, which does **not** include the subtraction of `size(tx_A) + size(tx_B)`.
5. `self.total_tx_size` is now inflated by `size(tx_A) + size(tx_B)`.
6. Subsequent calls to `limit_size` will evict additional legitimate transactions to bring the (falsely elevated) counter below `max_tx_pool_size`. [11](#0-10) [12](#0-11)

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

**File:** tx-pool/src/component/pool_map.rs (L252-265)
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

**File:** tx-pool/src/component/pool_map.rs (L710-729)
```rust
    /// Calculate size and cycles statistics for adding a tx.
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

**File:** tx-pool/src/component/pool_map.rs (L731-758)
```rust
    /// Update size and cycles statistics for remove tx
    /// cycles overflow is possible, currently obtaining cycles is not accurate
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

**File:** tx-pool/src/service.rs (L1083-1097)
```rust
        TxPoolInfo {
            tip_hash: tip_header.hash(),
            tip_number: tip_header.number(),
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

**File:** rpc/src/module/pool.rs (L612-635)
```rust
    fn send_transaction(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<H256> {
        let tx: packed::Transaction = tx.into();
        let tx: core::TransactionView = tx.into_view();

        self.check_output_validator(outputs_validator, &tx)?;

        let tx_pool = self.shared.tx_pool_controller();
        let submit_tx = tx_pool.submit_local_tx(tx.clone());

        if let Err(e) = submit_tx {
            error!("Send submit_tx request error {}", e);
            return Err(RPCError::ckb_internal_error(e));
        }

        let tx_hash = tx.hash();
        match submit_tx.unwrap() {
            Ok(_) => Ok(tx_hash.into()),
            Err(reject) => Err(RPCError::from_submit_transaction_reject(&reject)),
        }
    }
```
