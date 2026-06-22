### Title
`total_tx_size` / `total_tx_cycles` Overcounted When Evictions Occur During `add_entry` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new aggregate values for `total_tx_size` and `total_tx_cycles` are computed **before** any evictions take place inside `check_and_record_ancestors`, but are written back **after** those evictions have already correctly decremented the same fields. The stale pre-eviction snapshot overwrites the correctly-updated values, permanently inflating both counters by the total size/cycles of every evicted transaction.

---

### Finding Description

`add_entry` follows this sequence:

```rust
// Step 1 – snapshot the new totals (self.total_tx_size is NOT modified yet)
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Step 2 – may evict transactions via remove_entry_and_descendants,
//           which calls update_stat_for_remove_tx and correctly
//           DECREMENTS self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

// ... insert the new entry ...

// Step 3 – overwrites the correctly-decremented self.total_tx_size
//           with the stale pre-eviction snapshot
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` takes `&self` and returns `(self.total_tx_size + tx_size, self.total_tx_cycles + cycles)` without touching the fields. [1](#0-0) 

`check_and_record_ancestors` calls `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size` and `self.total_tx_cycles` for every evicted transaction. [2](#0-1) [3](#0-2) 

The final write-back at lines 218–219 then replaces the correctly-decremented value with the stale snapshot: [4](#0-3) 

**Concrete arithmetic:**

| Step | `self.total_tx_size` | local `total_tx_size` |
|---|---|---|
| Initial | `X` | — |
| After `updated_stat_for_add_tx` | `X` (unchanged) | `X + new_size` |
| After evicting `E` bytes in `check_and_record_ancestors` | `X − E` (correct) | `X + new_size` (stale) |
| After line 218 | `X + new_size` (**wrong**) | — |

Correct value should be `X − E + new_size`. The counter is inflated by `E` (the total size of all evicted transactions).

The eviction path is triggered when a new transaction has more ancestors than `max_ancestors_count` **and** the excess ancestors are all cell-dep parents (`cell_ref_parents`): [5](#0-4) 

The `recompute_total_stat` fallback only fires on **underflow** during removal, not on this overcount scenario: [6](#0-5) 

---

### Impact Explanation

1. **Pool size enforcement is broken.** `limit_size` compares `pool_map.total_tx_size` against `config.max_tx_pool_size`. An inflated counter causes the pool to believe it is over-limit when it is not, triggering unnecessary eviction of legitimate pending transactions. [7](#0-6) 

2. **New transactions are incorrectly rejected.** Subsequent calls to `updated_stat_for_add_tx` read the inflated `self.total_tx_size` and may return `Reject::Full` for transactions that would fit within the real pool budget. [8](#0-7) 

3. **Incorrect RPC reporting.** `TxPoolInfo.total_tx_size` exposed via `tx_pool_info` RPC is read directly from `pool_map.total_tx_size`, so callers (wallets, relayers) see a false pool occupancy. [9](#0-8) 

4. **Cumulative drift.** Each successful eviction-during-add inflates the counter further. An attacker can repeat the trigger to drive `total_tx_size` arbitrarily high, eventually causing the pool to reject all new transactions.

---

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged RPC caller (`send_transaction`). The attacker:

1. Submits many transactions that all use the same output as a **cell dep** (not as an input), creating a large set of `cell_ref_parents` for a target output.
2. Submits a transaction that **consumes** that output as an input. This transaction now has `ancestors_count > max_ancestors_count` with the excess being cell-dep parents, triggering the eviction loop.
3. Each such submission inflates `total_tx_size` by the cumulative size of evicted transactions.
4. Repeating this pattern drives the counter to the pool size limit, after which all new transactions are rejected with `Reject::Full`.

No special privilege, key material, or majority hash power is required. The attack is executable by any node peer or RPC user.

---

### Recommendation

Compute the new totals **after** `check_and_record_ancestors` completes, so that any evictions are already reflected in `self.total_tx_size` and `self.total_tx_cycles` before the new entry's contribution is added:

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
    // Pre-check for overflow only; do NOT capture the new totals yet.
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));

    // Compute totals AFTER evictions have already updated self.total_tx_size/cycles.
    self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
    self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
    Ok((true, evicts))
}
```

Alternatively, recompute the totals from scratch after `check_and_record_ancestors` using `recompute_total_stat` and then add the new entry's contribution.

---

### Proof of Concept

1. Configure a node with `max_ancestors_count = 25` (default).
2. Submit a root transaction `T0` with one output `O0`.
3. Submit 26 transactions `C1…C26`, each spending an independent input but cell-depping on `O0`. All are accepted (each has only 1 ancestor: itself).
4. Record `pool_info.total_tx_size = S` via RPC.
5. Submit transaction `T_consume` that spends `O0` as an input. This triggers `check_and_record_ancestors`: `ancestors_count = 27 > 25`, all excess are `cell_ref_parents`. Two transactions (e.g., `C1`, `C2`) are evicted via `remove_entry_and_descendants`, correctly decrementing `self.total_tx_size` by their sizes. Then line 218 overwrites with the stale snapshot.
6. Observe via RPC that `pool_info.total_tx_size` is now `S + size(T_consume)` instead of `S - size(C1) - size(C2) + size(T_consume)`.
7. Repeat steps 3–6 to accumulate inflation until `total_tx_size` exceeds `max_tx_pool_size`, at which point all subsequent `send_transaction` calls return `Reject::Full` even though the pool has ample real space.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-220)
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
        Ok((true, evicts))
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
