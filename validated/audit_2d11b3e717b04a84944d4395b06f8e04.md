### Title
`total_tx_size`/`total_tx_cycles` Inflated When `check_and_record_ancestors` Evicts Transactions During `add_entry` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new-entry size/cycles are pre-computed into local variables before `check_and_record_ancestors` runs. When that function evicts existing pool entries (calling `remove_entry_and_descendants` → `update_stat_for_remove_tx`), it correctly decrements `self.total_tx_size` and `self.total_tx_cycles`. However, immediately afterward, `add_entry` unconditionally overwrites those fields with the pre-computed local values, erasing the decrements. The result is that `total_tx_size` and `total_tx_cycles` are permanently inflated by the sizes and cycles of every evicted transaction. An unprivileged tx-pool submitter can craft a transaction chain that repeatedly triggers this path, causing the pool to believe it is full when it is not, and thereby denying service to all subsequent legitimate transaction submissions.

---

### Finding Description

In `tx-pool/src/component/pool_map.rs`, `add_entry` executes the following sequence:

```
// Step 1 – compute new totals into LOCAL variables (self is not yet modified)
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Step 2 – may call remove_entry_and_descendants → update_stat_for_remove_tx,
//           which DOES modify self.total_tx_size and self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

// Step 3 – OVERWRITES the decremented self fields with the pre-eviction locals
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` takes `&self` and returns `(self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles)` without touching `self`.

`check_and_record_ancestors` contains an eviction branch that fires when the incoming transaction's ancestor count exceeds `max_ancestors_count` but can be reduced to within the limit by removing "cell-ref parents" (ancestors that reference a cell as a dep rather than as an input). Inside that branch, `remove_entry_and_descendants` is called for each evicted ancestor, which calls `remove_entry`, which calls `update_stat_for_remove_tx`, which subtracts the evicted entry's `size` and `cycles` from `self.total_tx_size` and `self.total_tx_cycles`.

After `check_and_record_ancestors` returns, Step 3 blindly writes the stale local values back, undoing every decrement performed in Step 2. The net effect after one such insertion is:

```
self.total_tx_size  = old_total + new_entry_size          (wrong)
correct value       = old_total - Σ(evicted_sizes) + new_entry_size
inflation           = Σ(evicted_sizes)
```

Each successful trigger of this path permanently inflates the counters by the total size of the evicted transactions.

The eviction branch in `check_and_record_ancestors` is reachable whenever:
- `ancestors_count > max_ancestors_count` (default 25), AND
- `ancestors_count − cell_ref_parents.len() ≤ max_ancestors_count`

An attacker can engineer this by first submitting a chain of transactions where some use a shared cell as a `cell_dep` (making them "cell-ref parents"), then submitting a new transaction whose ancestor count exceeds the limit but whose cell-ref parents account for the excess.

---

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size` to decide whether to evict transactions from the pool:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee entries
}
```

An inflated `total_tx_size` causes `limit_size` to evict legitimate transactions that should remain in the pool, and causes `updated_stat_for_add_tx` to reject new submissions with `Reject::Full` even when the pool has ample real capacity. By repeatedly triggering the eviction path, an attacker can drive `total_tx_size` arbitrarily high relative to the actual pool contents, effectively making the tx-pool permanently reject all new transactions — a complete denial of service for transaction submission on the targeted node.

---

### Likelihood Explanation

Any unprivileged actor who can submit transactions to the tx-pool (via the `send_transaction` RPC or the P2P relay protocol) can trigger this path. The attacker needs only to:
1. Submit a chain of ~25 transactions where at least one uses a shared cell as a `cell_dep`.
2. Submit a new transaction that references those transactions as ancestors.

No privileged keys, no majority hashpower, and no social engineering are required. The attack is repeatable and cumulative.

---

### Recommendation

In `add_entry`, compute the new totals **after** `check_and_record_ancestors` completes, so that any evictions performed inside it are already reflected in `self.total_tx_size` and `self.total_tx_cycles` before the new entry's contribution is added:

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
    // Validate that adding this entry would not overflow, but do NOT
    // store the result yet — evictions inside check_and_record_ancestors
    // will change self.total_tx_size/cycles first.
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

    let evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));

    // Now apply the new entry's contribution on top of the post-eviction totals.
    self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
    self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
    Ok((true, evicts))
}
```

---

### Proof of Concept

**Root cause — `add_entry` overwrites post-eviction totals with pre-eviction locals:** [1](#0-0) 

**`updated_stat_for_add_tx` takes `&self` and returns new values without modifying `self`:** [2](#0-1) 

**`check_and_record_ancestors` eviction branch calls `remove_entry_and_descendants`, which calls `update_stat_for_remove_tx` and modifies `self.total_tx_size`/`total_tx_cycles`:** [3](#0-2) 

**`remove_entry` calls `update_stat_for_remove_tx`, which decrements `self.total_tx_size` and `self.total_tx_cycles`:** [4](#0-3) 

**`update_stat_for_remove_tx` — the decrement that gets overwritten:** [5](#0-4) 

**`limit_size` uses the inflated `total_tx_size` to drive unnecessary evictions and rejections:** [6](#0-5)

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
