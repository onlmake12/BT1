### Title
Stale Aggregate Stat Overwrite After In-Flight Eviction Inflates `total_tx_size`/`total_tx_cycles` — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::add_entry` pre-computes new aggregate totals before calling `check_and_record_ancestors`, which may itself evict pool entries and correctly decrement those totals. The pre-computed snapshot is then unconditionally written back, silently un-doing every decrement that happened during eviction. The result is that `total_tx_size` and `total_tx_cycles` are permanently inflated by the combined size/cycles of all evicted transactions, causing `limit_size` to over-evict legitimate pending transactions from the pool.

---

### Finding Description

In `add_entry`, the new aggregate totals are computed as a local snapshot **before** any evictions occur:

```rust
// pool_map.rs lines 210-211
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
```

`updated_stat_for_add_tx` simply returns `(self.total_tx_size + tx_size, self.total_tx_cycles + cycles)` — a point-in-time snapshot of the pre-eviction state plus the new entry. [1](#0-0) 

Immediately after, `check_and_record_ancestors` is called. When the new transaction has too many ancestors due to cell-dep relationships, it evicts existing pool entries via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which **correctly** decrements `self.total_tx_size` and `self.total_tx_cycles` for each evicted entry: [2](#0-1) [3](#0-2) 

But then, at the end of `add_entry`, the stale pre-eviction snapshot is unconditionally written back:

```rust
// pool_map.rs lines 218-219
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
``` [4](#0-3) 

This overwrites the correctly-decremented running totals with the pre-eviction snapshot, effectively un-doing every `update_stat_for_remove_tx` call that occurred inside `check_and_record_ancestors`. After `add_entry` returns, `total_tx_size` and `total_tx_cycles` are inflated by the combined size and cycles of all evicted transactions.

---

### Impact Explanation

`limit_size` enforces the pool size cap by comparing `total_tx_size` against `max_tx_pool_size`:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee-rate entries
}
``` [5](#0-4) 

With an inflated `total_tx_size`, the pool believes it is fuller than it actually is. `limit_size` then evicts additional legitimate pending or proposed transactions to bring the (phantom) total back under the cap. The evicted transactions receive a `Reject::Full` callback and are removed from the pool permanently. Honest users whose transactions are evicted must resubmit, and may be unable to get their transactions included if the attacker repeats the trigger.

Additionally, `total_tx_cycles` is exposed via the `get_tip_tx_pool_info` RPC and used in fee-rate estimation: [6](#0-5) 

An inflated `total_tx_cycles` causes the fee estimator to return artificially high fee rates, misleading users into overpaying.

---

### Likelihood Explanation

The eviction path inside `check_and_record_ancestors` is triggered when:
1. A new transaction's ancestor count (counting cell-dep parents) exceeds `max_ancestors_count`, AND
2. Removing some cell-dep-only parents would bring the count within the limit. [7](#0-6) 

An unprivileged tx-pool submitter can deliberately construct this scenario:
- Submit a set of transactions where some act as cell-dep ancestors of others, building up a deep cell-dep chain in the pool.
- Submit a new transaction that references many of these as cell-dep ancestors, exceeding `max_ancestors_count`.
- The eviction fires, inflating the totals.
- Repeat to accumulate inflation across multiple submissions.

This requires no special privilege — only the ability to submit transactions via RPC or P2P relay, which is available to any network participant.

---

### Recommendation

Compute the new aggregate totals **after** `check_and_record_ancestors` returns, so that any decrements applied during eviction are already reflected in `self.total_tx_size` and `self.total_tx_cycles` before the new entry's contribution is added:

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
    // Validate that adding this entry won't overflow, but do NOT commit yet.
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));

    // Apply the new entry's contribution AFTER evictions have already
    // decremented the running totals.
    self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
    self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
    Ok((true, evicts))
}
```

Alternatively, move the overflow check into a dry-run that does not capture a snapshot, and always derive the final totals from the live fields after eviction.

---

### Proof of Concept

**Setup:** Pool with `max_tx_pool_size = 1000` bytes and `max_ancestors_count = 3`. Pool currently holds transactions A, B, C (total size = 900 bytes), where B and C are cell-dep ancestors of a future transaction.

**Step 1:** Attacker submits transaction D, which has cell-dep ancestors {A, B, C}, making `ancestors_count = 4 > max_ancestors_count = 3`. The cell-dep-only parents are {B, C} (2 entries), so `4 - 2 = 2 <= 3`, triggering the eviction path.

**Step 2:** Inside `check_and_record_ancestors`, B (size=200) is evicted:
- `update_stat_for_remove_tx(200, ...)` → `self.total_tx_size = 900 - 200 = 700`

**Step 3:** Back in `add_entry`, the stale snapshot `total_tx_size = 900 + size(D)` is written back, e.g. `900 + 150 = 1050`.

**Step 4:** `limit_size` fires because `1050 > 1000`. It evicts A (size=500), bringing the phantom total to `1050 - 500 = 550`. But the real pool size is only `700 - 500 + 150 = 350` bytes — well under the cap. A was evicted unnecessarily.

**Step 5:** Repeat with fresh transactions to continuously drain the pool of legitimate entries. [4](#0-3) [8](#0-7) [5](#0-4)

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
