### Title
Stale Pre-Eviction Totals Overwrite In-Place Accounting in `add_entry`, Inflating `total_tx_size`/`total_tx_cycles` — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new `total_tx_size` and `total_tx_cycles` are computed as **local variables** before `check_and_record_ancestors` runs. When that function evicts cell-ref-parent transactions via `remove_entry_and_descendants → remove_entry → update_stat_for_remove_tx`, it correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in place. However, the final two lines of `add_entry` then **overwrite** those in-place decrements with the stale pre-eviction snapshot, permanently inflating both counters by the size and cycles of every evicted transaction.

---

### Finding Description

`add_entry` in `pool_map.rs` follows this sequence:

```rust
// Step 1: snapshot new totals into LOCAL variables (self.* not yet modified)
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Step 2: may call remove_entry_and_descendants → update_stat_for_remove_tx
//         which MODIFIES self.total_tx_size / self.total_tx_cycles in-place
evicts = self.check_and_record_ancestors(&mut entry)?;

self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Step 3: OVERWRITES the in-place decrements from Step 2
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

`updated_stat_for_add_tx` only reads `self.total_tx_size` and returns a new value; it does not write back: [2](#0-1) 

`check_and_record_ancestors` can call `remove_entry_and_descendants`, which calls `remove_entry`, which calls `update_stat_for_remove_tx` and **does** write back to `self.total_tx_size`: [3](#0-2) [4](#0-3) 

The eviction path inside `check_and_record_ancestors` is triggered when a new transaction's ancestor count exceeds `max_ancestors_count` but can be reduced by evicting cell-ref-parent transactions: [5](#0-4) 

**Concrete accounting error:**

| Step | `self.total_tx_size` | local `total_tx_size` |
|---|---|---|
| Initial | `X` | — |
| After `updated_stat_for_add_tx(entry_size)` | `X` (unchanged) | `X + entry_size` |
| After eviction of tx with `evicted_size` | `X − evicted_size` | `X + entry_size` (stale) |
| After `self.total_tx_size = total_tx_size` | `X + entry_size` (**wrong**) | — |

Correct value: `X − evicted_size + entry_size`. Inflation: `evicted_size`.

---

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size` to decide whether to evict further transactions: [6](#0-5) 

Because `total_tx_size` is inflated by the size of every cell-ref-parent eviction, `limit_size` will see the pool as over-capacity even when it is not, and will evict additional valid pending/proposed transactions. Those transactions are permanently removed from the pool (callbacks fire `Reject`) and must be resubmitted by their originators. The same inflation is reported to RPC callers via `get_pool_info`: [7](#0-6) 

**Impact: Medium–High.** Legitimate transactions are silently evicted from the mempool. Miners lose fee revenue. Users experience unexpected transaction drops without a consensus-level error.

---

### Likelihood Explanation

The trigger condition — a new transaction whose ancestor count exceeds `max_ancestors_count` only because of cell-ref-parent transactions — is reachable by any unprivileged `send_raw_transaction` RPC caller. An attacker can:

1. Pre-populate the pool with a chain of transactions that share a common cell dep (creating cell-ref-parent relationships).
2. Submit a new transaction that references those outputs, pushing `ancestors_count` just above `max_ancestors_count` while `ancestors_count − cell_ref_parents.len() ≤ max_ancestors_count`.
3. Each such submission inflates `total_tx_size` by the evicted transactions' sizes.
4. Repeat to accumulate inflation until `limit_size` begins evicting honest transactions.

No privileged access, key material, or majority hashpower is required.

---

### Recommendation

Move the stat update to **after** `check_and_record_ancestors` completes, so it incorporates any in-place decrements made by evictions:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Compute totals AFTER evictions have already modified self.total_tx_size
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, replace the local-variable pattern with direct in-place increments after all mutations are complete, mirroring the subtract-then-recompute pattern already used in `update_stat_for_remove_tx`.

---

### Proof of Concept

```
Initial pool: total_tx_size = 180_000 bytes, max_tx_pool_size = 200_000 bytes

1. Attacker submits tx_A (size 5_000) and tx_B (size 5_000) that both reference
   the same cell dep output O. Both enter the pool.
   total_tx_size = 190_000

2. Attacker submits tx_C (size 1_000) whose input spends O, making tx_A and tx_B
   cell-ref-parents of tx_C. ancestors_count = 3 > max_ancestors_count = 2,
   but 3 - 2 (cell_ref_parents) = 1 ≤ 2, so eviction path fires.

3. check_and_record_ancestors evicts tx_A (5_000 bytes):
   update_stat_for_remove_tx(5_000) → self.total_tx_size = 185_000

4. add_entry overwrites: self.total_tx_size = total_tx_size = 191_000
   (pre-eviction 190_000 + entry 1_000, ignoring the −5_000 eviction)

5. Correct value: 190_000 − 5_000 + 1_000 = 186_000
   Actual value:  191_000  (inflated by 5_000)

6. limit_size sees 191_000 > 200_000? Not yet. Repeat step 2 with more
   cell-ref-parent evictions until inflation crosses the threshold, at which
   point limit_size evicts honest transactions from the pool.
``` [8](#0-7) [9](#0-8)

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

**File:** tx-pool/src/pool.rs (L292-327)
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
