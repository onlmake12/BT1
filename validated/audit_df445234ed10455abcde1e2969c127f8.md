### Title
Stale Pool Size Accounting After Ancestor-Eviction in `add_entry` Inflates `total_tx_size`/`total_tx_cycles` — (`tx-pool/src/component/pool_map.rs`)

### Summary

In `PoolMap::add_entry`, the new transaction's contribution to `total_tx_size` and `total_tx_cycles` is computed into local variables **before** `check_and_record_ancestors` is called. When that function evicts existing transactions to satisfy the ancestor-count limit, each eviction correctly calls `update_stat_for_remove_tx`, which decrements `self.total_tx_size` and `self.total_tx_cycles` in place. However, `add_entry` then unconditionally overwrites those fields with the stale pre-eviction locals, erasing the decrements. The pool's size counters become permanently inflated by the byte-size and cycle-count of every evicted transaction, causing `limit_size` to over-evict valid transactions and `updated_stat_for_add_tx` to reject new submissions as `Reject::Full` even when the pool has real capacity.

### Finding Description

**Root cause — `PoolMap::add_entry` (`tx-pool/src/component/pool_map.rs`, lines 200–221):**

```
Step 1 (line 210-211): local vars = self.total_tx_size + entry.size
                                     self.total_tx_cycles + entry.cycles

Step 2 (line 213):     check_and_record_ancestors() may call
                       remove_entry_and_descendants() → remove_entry()
                       → update_stat_for_remove_tx()
                       which WRITES self.total_tx_size -= evicted_size
                                    self.total_tx_cycles -= evicted_cycles

Step 3 (lines 218-219): self.total_tx_size  = local_var  ← OVERWRITES step-2 result
                         self.total_tx_cycles = local_var  ← OVERWRITES step-2 result
``` [1](#0-0) 

The eviction path inside `check_and_record_ancestors` is reached when a new transaction's ancestor count (including ancestors reachable through cell-dep references) exceeds `max_ancestors_count`, but can be reduced to within the limit by evicting the conflicting `cell_ref_parents`: [2](#0-1) 

Each evicted entry goes through `remove_entry`, which calls `update_stat_for_remove_tx` and correctly decrements the live fields: [3](#0-2) 

But those decrements are immediately discarded when `add_entry` writes back the stale locals: [4](#0-3) 

**Concrete arithmetic:**
- Pool state before: `total_tx_size = S`, pool contains transactions summing to `S`.
- New tx size = `N`. Local var = `S + N`.
- Eviction removes transactions totalling `E` bytes → `self.total_tx_size` is correctly updated to `S − E`.
- Final write: `self.total_tx_size = S + N` (stale). Correct value: `S − E + N`.
- Inflation per trigger: `E` bytes (and the corresponding cycles).

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size` to decide whether to keep evicting: [5](#0-4) 

And it is the sole guard used by `updated_stat_for_add_tx` to reject incoming transactions as `Reject::Full`: [6](#0-5) 

With an inflated counter:

1. **Over-eviction DoS**: `limit_size` (called after every `submit_entry`) will evict additional valid, high-fee transactions that would otherwise fit, because the counter says the pool is over-limit when it is not.
2. **Admission DoS**: `updated_stat_for_add_tx` will reject new transactions with `Reject::Full` even when the pool has real capacity, because the inflated counter makes the pool appear full.
3. **Incorrect RPC reporting**: `tx_pool_info` returns the inflated `total_tx_size` and `total_tx_cycles` to callers.

The inflation is **cumulative and permanent** across repeated triggers — each successful trigger adds `E` to the counter with no self-correcting mechanism (the `recompute_total_stat` fallback in `update_stat_for_remove_tx` is only reached on underflow, not on the overwrite path). [7](#0-6) 

### Likelihood Explanation

The trigger condition — a new transaction whose cell-dep output is also consumed as an input by an existing pool transaction, pushing the ancestor count over `max_ancestors_count` — is reachable by any unprivileged transaction sender via the standard `send_transaction` RPC. No privileged keys, majority hashpower, or Sybil capability is required. An attacker can craft a sequence of transactions that repeatedly hits this path, accumulating inflation with each submission.

### Recommendation

Compute the final `total_tx_size` and `total_tx_cycles` **after** `check_and_record_ancestors` returns, rather than capturing them before. One correct approach:

```rust
// After check_and_record_ancestors (which may have evicted entries and
// already decremented self.total_tx_size / self.total_tx_cycles):
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, move the `updated_stat_for_add_tx` call to after `check_and_record_ancestors` so the base values it reads are already post-eviction.

### Proof of Concept

1. Fill the pool with a chain of `max_ancestors_count − 1` transactions `T1 → T2 → … → T_{N-1}`, where each `Ti` spends an output of `T_{i-1}`. Each has size `S`.
2. Submit a transaction `A` that spends an unrelated UTXO **and** uses the output of `T1` as a cell dep. `A`'s ancestor count = `N` (exceeds `max_ancestors_count`). `T1` is a `cell_ref_parent` and gets evicted along with its descendants.
3. After step 2, `total_tx_size` should equal `1 * S` (only `A` remains), but instead equals `(N-1)*S + size(A)` — inflated by `(N-2)*S`.
4. Repeat step 2 with fresh transactions. Each iteration inflates `total_tx_size` further.
5. After enough iterations, `total_tx_size > max_tx_pool_size` permanently, causing `limit_size` to evict every newly submitted transaction and `updated_stat_for_add_tx` to reject all new submissions as `Reject::Full`. [8](#0-7) [9](#0-8) [10](#0-9)

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

**File:** tx-pool/src/component/pool_map.rs (L711-728)
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
```

**File:** tx-pool/src/pool.rs (L290-329)
```rust
    // Remove transactions from the pool until total size <= size_limit.
    // Return a `Reject` for current inserting entry if it's removed
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
