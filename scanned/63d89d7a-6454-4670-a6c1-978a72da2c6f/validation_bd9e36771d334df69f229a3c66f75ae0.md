### Title
`total_tx_size`/`total_tx_cycles` Accounting Corrupted When `add_entry` Evicts Ancestors — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new pool-size accounting values are computed **before** `check_and_record_ancestors` runs. That inner call can evict existing pool entries (via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`), which correctly decrements `self.total_tx_size` / `self.total_tx_cycles`. Immediately afterward, `add_entry` **overwrites** those fields with the stale pre-eviction snapshot, silently re-inflating the totals by the size and cycles of every evicted transaction. The result is a persistent, growing overcount that causes `limit_size` to evict legitimate transactions unnecessarily and causes `tx_pool_info` to report a pool that is larger than reality.

---

### Finding Description

`PoolMap::add_entry` (lines 200–221) follows this sequence:

```
// Step 1 – snapshot future totals BEFORE any evictions
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
//   = (self.total_tx_size + entry.size,
//      self.total_tx_cycles + entry.cycles)

// Step 2 – may call remove_entry_and_descendants → remove_entry
//           → update_stat_for_remove_tx, which DOES mutate
//           self.total_tx_size and self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

// Step 3 – OVERWRITE with the stale pre-eviction snapshot
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

`updated_stat_for_add_tx` is a pure read that returns `(self.total_tx_size + tx_size, self.total_tx_cycles + cycles)` without touching `self`: [2](#0-1) 

`remove_entry` (called transitively from `check_and_record_ancestors`) **does** mutate `self.total_tx_size` / `self.total_tx_cycles` via `update_stat_for_remove_tx`: [3](#0-2) 

The eviction path inside `check_and_record_ancestors` is triggered when the incoming transaction has more ancestors than `max_ancestors_count` but the excess is entirely attributable to "cell-ref parents" (pool transactions that hold a cell dep whose output the new transaction wants to consume as an input): [4](#0-3) 

When N entries are evicted, the correct final value of `total_tx_size` is:

```
original − Σ(evicted[i].size) + entry.size
```

But Step 3 writes:

```
original + entry.size          ← Σ(evicted[i].size) is never subtracted
```

Every invocation of `add_entry` that triggers the eviction path inflates `total_tx_size` and `total_tx_cycles` by the aggregate size/cycles of the evicted entries.

---

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size` to decide whether to evict further transactions: [5](#0-4) 

An inflated `total_tx_size` causes `limit_size` to believe the pool is over capacity and to evict additional (legitimate, higher-fee) transactions that should have remained. This can be exploited to selectively purge targeted transactions from the mempool. Additionally, `tx_pool_info` (consumed by miners, relayers, and monitoring tools) reports the corrupted counters: [6](#0-5) 

Concrete consequences:
1. **Forced eviction of legitimate transactions** — `limit_size` evicts real entries to compensate for phantom size.
2. **False `Reject::Full` rejections** — new transactions are refused even when actual pool occupancy is below `max_tx_pool_size`.
3. **Incorrect `tx_pool_info` RPC output** — miners and relayers see a pool that appears larger than it is, potentially causing them to under-fill blocks or apply incorrect fee-rate estimates.

---

### Likelihood Explanation

The eviction path requires a transaction whose ancestor count exceeds `max_ancestors_count` (default 25) but can be reduced to within the limit by removing "cell-ref parents." An unprivileged transaction submitter can craft this scenario by:

1. Submitting a chain of 26+ dependent transactions (T₁ → T₂ → … → T₂₆).
2. Submitting several transactions that reference an output of T₁ as a **cell dep** (creating cell-ref parents).
3. Submitting a new transaction that spends T₂₆'s output (inheriting 26 ancestors) and also spends the same output that the cell-dep transactions reference, making those cell-dep transactions evictable.

This is fully achievable by any RPC caller or P2P transaction sender without any privileged access.

---

### Recommendation

Move the accounting snapshot **after** `check_and_record_ancestors` completes, so it reflects the post-eviction state:

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

    // Validate capacity BEFORE mutating state (overflow check only)
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

    let evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));

    // Recompute AFTER evictions have already updated self.total_tx_size/cycles
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    self.total_tx_size = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;

    Ok((true, evicts))
}
```

Alternatively, replace the snapshot pattern with direct in-place increments applied only after all evictions are complete.

---

### Proof of Concept

1. Fill the pool with a chain T₁ → T₂ → … → T₂₆ (26 transactions, each spending the previous output).
2. Submit transactions D₁, D₂, D₃ that each use T₁'s output as a **cell dep** (these become cell-ref parents of any transaction that spends T₁'s output).
3. Submit T_new that spends T₂₆'s output (26 ancestors) and also spends T₁'s output directly. `ancestors_count = 27 > 25`; `cell_ref_parents = {D₁, D₂, D₃}`; `27 − 3 = 24 ≤ 25` → eviction path is entered.
4. `check_and_record_ancestors` calls `remove_entry_and_descendants` for D₁, D₂, D₃, correctly decrementing `self.total_tx_size` by `size(D₁) + size(D₂) + size(D₃)`.
5. Step 3 of `add_entry` overwrites `self.total_tx_size` with the pre-eviction snapshot, re-adding `size(D₁) + size(D₂) + size(D₃)` as phantom bytes.
6. Observe via `tx_pool_info` RPC that `total_tx_size` is inflated. Repeat to accumulate inflation until `limit_size` begins evicting legitimate transactions.

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
