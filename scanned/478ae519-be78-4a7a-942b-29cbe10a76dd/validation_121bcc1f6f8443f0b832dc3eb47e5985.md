### Title
Tx-Pool `total_tx_size`/`total_tx_cycles` Accounting Mismatch Due to Pre-Eviction Snapshot Overwrite — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::add_entry` computes updated `total_tx_size` and `total_tx_cycles` **before** calling `check_and_record_ancestors`, which may internally evict existing transactions (correctly subtracting their sizes/cycles). The pre-eviction snapshot is then unconditionally written back, silently discarding the eviction corrections. This causes `total_tx_size` and `total_tx_cycles` to be permanently inflated by the size and cycles of every evicted transaction, diverging from the actual pool contents — the same class of internal-accounting-vs-actual-state mismatch as the reference report.

---

### Finding Description

In `PoolMap::add_entry`:

```rust
// Step 1 — snapshot computed BEFORE any evictions
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Step 2 — may evict N transactions; each eviction calls
//           update_stat_for_remove_tx, correctly adjusting
//           self.total_tx_size and self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Step 3 — OVERWRITES the corrected values with the pre-eviction snapshot
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

`updated_stat_for_add_tx` simply returns `(self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles)` — it does not account for any evictions that have not yet occurred. [2](#0-1) 

`check_and_record_ancestors` evicts transactions when the incoming transaction's ancestor count exceeds `max_ancestors_count` but can be brought under the limit by removing cell-dep-referencing parents. Each eviction goes through `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which correctly subtracts the evicted transaction's `size` and `cycles` from `self.total_tx_size` / `self.total_tx_cycles`. [3](#0-2) [4](#0-3) 

After `check_and_record_ancestors` returns, the corrected totals are immediately overwritten by the stale snapshot from Step 1. The net effect for each evicted transaction of size `e_size` / cycles `e_cycles`:

```
Actual pool total_tx_size  = old_total - e_size  + entry.size
Tracked total_tx_size      = old_total            + entry.size   ← inflated by e_size
```

The code even acknowledges the fragility of this accounting with the comment:

> `/// cycles overflow is possible, currently obtaining cycles is not accurate` [5](#0-4) 

The `PoolMap` struct stores these two counters as the authoritative pool-size state: [6](#0-5) 

---

### Impact Explanation

**Pool size enforcement is corrupted.** `limit_size` evicts transactions whenever `total_tx_size > max_tx_pool_size`: [7](#0-6) 

Because `total_tx_size` is inflated by the cumulative size of all transactions evicted via the ancestor-limit path, the pool believes it is over capacity when it is not. This causes `limit_size` to evict additional legitimate pending/proposed transactions that would otherwise remain, permanently shrinking the effective pool capacity below `max_tx_pool_size`.

**RPC reporting is misleading.** `tx_pool_info` reads `total_tx_size` and `total_tx_cycles` directly: [8](#0-7) 

Callers (miners, wallets, fee estimators) receive inflated values, causing them to over-estimate pool congestion and set unnecessarily high fee rates.

---

### Likelihood Explanation

The eviction path inside `check_and_record_ancestors` is triggered when:
1. A submitted transaction has more ancestors than `max_ancestors_count`, **and**
2. Reducing the count by removing cell-dep-referencing parents brings it within the limit.

An unprivileged `send_transaction` RPC caller can craft a transaction chain that deliberately references cell deps already in the pool, satisfying both conditions. No special privilege, key material, or majority hash power is required. The attacker submits a sequence of such transactions; each one that triggers an eviction inflates the counters by the evicted transaction's size/cycles. The inflation is permanent until the node restarts or `clear_tx_pool` is called.

---

### Recommendation

Compute the pre-eviction snapshot **after** `check_and_record_ancestors` completes, or accumulate the evicted sizes/cycles and subtract them from the snapshot before writing back:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;

// Compute totals AFTER evictions have already adjusted self.total_tx_size/cycles
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, remove the final overwrite entirely and let `update_stat_for_remove_tx` (for evictions) and a single `checked_add` for the new entry maintain the counters incrementally throughout the function.

---

### Proof of Concept

**Setup**: pool with `max_ancestors_count = 25`, `max_tx_pool_size = 10 MB`. Pool currently holds 24 transactions in a chain (tx₁ → tx₂ → … → tx₂₄), each 100 KB. `total_tx_size = 2,400,000`.

**Attack**:
1. Submit tx₂₅ that spends an output of tx₁ **and** references tx₁₂ as a cell dep. Ancestor count = 25 (at limit). Cell-dep parent = tx₁₂.
2. `add_entry` is called:
   - Step 1: `total_tx_size_snapshot = 2,400,000 + 100,000 = 2,500,000`
   - Step 2: `check_and_record_ancestors` evicts tx₁₂ and its descendants (tx₁₃…tx₂₄ = 13 txs × 100 KB = 1,300,000 bytes). `self.total_tx_size` is correctly updated to `2,400,000 − 1,300,000 = 1,100,000`.
   - Step 3: `self.total_tx_size = 2,500,000` ← overwrites the correct value.
3. Actual pool size: `1,100,000 + 100,000 = 1,200,000` bytes.
4. Tracked `total_tx_size`: `2,500,000` bytes — inflated by **1,300,000 bytes**.
5. `limit_size` now sees `2,500,000 > max_tx_pool_size` (if limit is, say, 2 MB) and begins evicting legitimate transactions that should have remained.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L60-75)
```rust
pub struct PoolMap {
    /// The pool entries with different kinds of sort strategies
    pub(crate) entries: MultiIndexPoolEntryMap,
    /// All the deps, header_deps, inputs, outputs relationships
    pub(crate) edges: Edges,
    /// All the parent/children relationships
    pub(crate) links: TxLinksMap,
    pub(crate) max_ancestors_count: usize,
    // sum of all tx_pool tx's virtual sizes.
    pub(crate) total_tx_size: usize,
    // sum of all tx_pool tx's cycles.
    pub(crate) total_tx_cycles: Cycle,
    pub(crate) pending_count: usize,
    pub(crate) gap_count: usize,
    pub(crate) proposed_count: usize,
}
```

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
