### Title
Stale Pre-Computed `total_tx_size`/`total_tx_cycles` Overwrite Causes Inflated Pool Accounting After Ancestor-Eviction — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new aggregate pool statistics (`total_tx_size`, `total_tx_cycles`) are computed **before** the ancestor-eviction path runs. If `check_and_record_ancestors` evicts transactions (calling `update_stat_for_remove_tx` to decrement the live counters), those decrements are silently discarded when the pre-computed stale values are written back. This is the direct CKB analog of the "double locking" class: a running total is incremented without first accounting for a concurrent decrement, causing the counter to be permanently inflated.

---

### Finding Description

`add_entry` in `tx-pool/src/component/pool_map.rs` follows this sequence:

```rust
// Step 1 – snapshot future totals BEFORE any eviction
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // = self.total_tx_size + entry.size

// Step 2 – may evict pool entries, each calling update_stat_for_remove_tx
//           which DECREMENTS self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Step 3 – OVERWRITE with the stale snapshot, discarding the decrements from Step 2
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

`updated_stat_for_add_tx` takes `&self` (immutable) and returns `self.total_tx_size + entry.size` as a plain integer — it does not modify the field. [2](#0-1) 

`check_and_record_ancestors` contains an eviction branch that is entered when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. Inside that branch it calls `remove_entry_and_descendants`, which calls `remove_entry`, which calls `update_stat_for_remove_tx` — directly decrementing `self.total_tx_size` and `self.total_tx_cycles`. [3](#0-2) [4](#0-3) [5](#0-4) 

After the eviction, `self.total_tx_size` has been correctly reduced by the evicted sizes. But then line 218 overwrites it with the pre-eviction snapshot (`old_total + entry.size`), erasing those reductions. The net result:

```
correct value  = old_total − Σ(evicted_sizes) + entry.size
actual value   = old_total                    + entry.size
inflation      = Σ(evicted_sizes)
```

Every invocation of the eviction path permanently inflates `total_tx_size` by the sum of the evicted entries' sizes. The same applies to `total_tx_cycles`.

---

### Impact Explanation

`total_tx_size` is the sole gate for pool admission and eviction:

- `limit_size` loops while `total_tx_size > max_tx_pool_size`, evicting real transactions to bring the (already-inflated) counter down. This causes legitimate transactions to be expelled from the pool unnecessarily.
- `updated_stat_for_add_tx` rejects new submissions with `Reject::Full` when `total_tx_size + new_size` overflows or exceeds the limit — even if the pool has ample real space. [6](#0-5) 

A sufficiently inflated counter causes the tx-pool to permanently reject all incoming transactions with `Reject::Full`, constituting a denial-of-service against the local node's transaction relay and block-template assembly.

---

### Likelihood Explanation

The eviction branch is reachable by any unprivileged tx-pool submitter (local RPC `send_transaction` or remote relay). The attacker must:

1. Submit a chain of transactions where multiple ancestors reference the same cell dep (making them `cell_ref_parents` of the new tx).
2. Submit a new transaction whose ancestor count exceeds `max_ancestors_count` (default 25) but whose non-cell-ref ancestor count does not.
3. Each such submission that triggers the eviction path inflates `total_tx_size` by the sizes of the evicted entries.
4. Repeating this inflates the counter until the pool rejects all further submissions.

No privileged access, key material, or majority hashpower is required. The attack requires only the ability to submit transactions to the pool.

---

### Recommendation

Compute the new aggregate totals **after** `check_and_record_ancestors` completes (so any eviction-driven decrements to `self.total_tx_size` are already reflected), or re-derive the totals from the live field at that point:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Compute AFTER evictions have already updated self.total_tx_size
self.total_tx_size = self.total_tx_size.checked_add(entry.size)
    .ok_or_else(|| Reject::Full(...))?;
self.total_tx_cycles = self.total_tx_cycles.checked_add(entry.cycles)
    .ok_or_else(|| Reject::Full(...))?;
```

Alternatively, move the overflow check to after the eviction path and derive the final values from the already-updated `self.total_tx_size`.

---

### Proof of Concept

1. Attacker pre-populates the pool with 26 transactions `T1…T26`, each referencing the same cell dep `C` (making all of them `cell_ref_parents` for any subsequent tx that also references `C`).
2. Attacker submits `T_new` whose inputs spend outputs of `T1…T26` and whose cell deps include `C`. `ancestors_count = 27 > 25 = max_ancestors_count`; `cell_ref_parents = {T1…T26}`; `27 − 26 = 1 ≤ 25`, so the eviction branch fires.
3. One entry (e.g., `T1`, size = S) is evicted: `self.total_tx_size -= S`. Then line 218 overwrites with the pre-eviction snapshot, restoring `total_tx_size` to `old_total + size(T_new)` — `S` shannons of inflation remain.
4. Repeating step 2 with fresh transactions accumulates inflation. Once `total_tx_size` exceeds `max_tx_pool_size`, every subsequent `send_transaction` RPC call returns `Reject::Full`, blocking the node's mempool indefinitely without any chain-level attack. [7](#0-6) [8](#0-7)

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

**File:** tx-pool/src/pool.rs (L297-329)
```rust
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
