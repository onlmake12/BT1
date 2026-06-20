### Title
`PoolMap::total_tx_size` / `total_tx_cycles` Inflated When Ancestor-Eviction Occurs in `add_entry` — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new running totals for `total_tx_size` and `total_tx_cycles` are computed **before** a potential in-place eviction of ancestor entries. When `check_and_record_ancestors` evicts entries (calling `remove_entry_and_descendants` → `update_stat_for_remove_tx`), those decrements are applied to `self.total_tx_size` / `self.total_tx_cycles` in place. However, the final two lines of `add_entry` then **overwrite** those fields with the pre-eviction snapshot, silently discarding the decrements. The result is that `total_tx_size` and `total_tx_cycles` are permanently inflated by the sum of the evicted entries' sizes and cycles.

---

### Finding Description

`PoolMap::add_entry` follows this sequence: [1](#0-0) 

1. **Step 1** — `updated_stat_for_add_tx` reads the current `self.total_tx_size` and `self.total_tx_cycles` and returns a snapshot incremented by the new entry's values. These are stored in local variables `total_tx_size` / `total_tx_cycles`. [2](#0-1) 

2. **Step 2** — `check_and_record_ancestors` may evict existing pool entries when the ancestor count exceeds `max_ancestors_count` but can be reduced by removing `cell_ref_parents`. It calls `remove_entry_and_descendants`, which calls `remove_entry`, which calls `update_stat_for_remove_tx` — **modifying `self.total_tx_size` and `self.total_tx_cycles` in place**. [3](#0-2) 

3. **Step 3** — After the eviction, the final two lines unconditionally overwrite the fields with the pre-eviction snapshot: [4](#0-3) 

```
self.total_tx_size = total_tx_size;   // pre-eviction value + new entry size
self.total_tx_cycles = total_tx_cycles; // pre-eviction value + new entry cycles
```

The decrements applied by `update_stat_for_remove_tx` during eviction are silently discarded. The correct post-eviction value should be `(original − evicted_sizes) + new_entry_size`, but the actual stored value is `original + new_entry_size`. [5](#0-4) 

---

### Impact Explanation

`total_tx_size` is the sole metric used by `limit_size` to enforce `max_tx_pool_size`: [6](#0-5) 

An inflated `total_tx_size` causes `limit_size` to believe the pool is over capacity when it is not, triggering unnecessary eviction of legitimate pending/proposed transactions. This degrades tx-pool throughput and can cause valid user transactions to be silently dropped. The inflated value is also exposed via the `tx_pool_info` RPC: [7](#0-6) 

---

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is triggered when a submitted transaction has more ancestors than `max_ancestors_count`, but the excess is entirely due to `cell_ref_parents` (transactions that reference the same cell as a dep). An unprivileged tx-pool submitter reachable via RPC (`send_transaction`) or P2P relay can craft a transaction chain that hits this condition. Each such submission permanently inflates `total_tx_size` by the sum of the evicted entries' sizes, and the inflation accumulates across multiple such submissions.

---

### Recommendation

Move the computation of `total_tx_size` / `total_tx_cycles` to **after** `check_and_record_ancestors` completes, so the snapshot reflects the post-eviction state before adding the new entry:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
// Now compute totals from the already-updated self.total_tx_size
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
...
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

---

### Proof of Concept

1. Fill the pool with a chain of `max_ancestors_count` transactions `T1 → T2 → … → Tn`, where `T1` also has a cell dep consumed by a low-fee transaction `D` already in the pool (`D` becomes a `cell_ref_parent`).
2. Submit a new transaction `Tnew` that spends `Tn`'s output. Its ancestor count is `n + 1 > max_ancestors_count`, but `n + 1 - 1 (D) ≤ max_ancestors_count`, so the eviction branch fires.
3. `check_and_record_ancestors` evicts `D` (size `S_D`), calling `update_stat_for_remove_tx(S_D, C_D)`, which decrements `self.total_tx_size` by `S_D`.
4. The final `self.total_tx_size = total_tx_size` overwrites with the pre-eviction snapshot, restoring the `S_D` that was just subtracted.
5. After the call, `pool_map.total_tx_size` is inflated by `S_D`. Repeat to accumulate inflation until `limit_size` begins evicting legitimate transactions. [1](#0-0)

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
