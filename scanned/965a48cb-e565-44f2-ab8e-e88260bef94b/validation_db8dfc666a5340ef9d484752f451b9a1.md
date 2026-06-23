### Title
`total_tx_size` / `total_tx_cycles` Inflated After Ancestor-Eviction in `add_entry` — DoS on Tx-Pool Size Accounting (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::add_entry` pre-computes the new `total_tx_size` / `total_tx_cycles` before it may evict existing entries inside `check_and_record_ancestors`. When evictions do occur, `update_stat_for_remove_tx` correctly decrements the running totals, but the final two lines of `add_entry` then overwrite those corrected totals with the stale pre-eviction snapshot. The result is that `total_tx_size` (and `total_tx_cycles`) permanently over-counts the pool's real occupancy by the size of every evicted entry. An unprivileged tx-pool submitter can exploit this to make the node believe the pool is full when it is not, causing `limit_size` to evict legitimate third-party transactions — a targeted DoS on the mempool.

---

### Finding Description

In `add_entry`:

```
Step 1  (line 210-211): total_tx_size = self.total_tx_size + entry.size
                         (snapshot taken BEFORE any evictions)

Step 2  (line 213):      evicts = self.check_and_record_ancestors(&mut entry)?
                         → may call remove_entry_and_descendants
                           → remove_entry → update_stat_for_remove_tx
                           → self.total_tx_size -= evicted_entry.size   ← correct decrement

Step 3  (lines 218-219): self.total_tx_size = total_tx_size             ← OVERWRITES the
                         self.total_tx_cycles = total_tx_cycles            corrected value
                                                                           with the stale snapshot
```

After the overwrite, `self.total_tx_size` equals `old_total + entry.size` instead of the correct `old_total − Σ(evicted sizes) + entry.size`. Every invocation of the eviction branch inside `check_and_record_ancestors` inflates the counter by `Σ(evicted sizes)`.

The eviction branch is reached when a new transaction has more cell-ref ancestors than `max_ancestors_count` but can be brought within the limit by removing some of those cell-ref parents:

```rust
if ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count {
    while ancestors_count > self.max_ancestors_count {
        let removed = self.remove_entry_and_descendants(next_id);
        // update_stat_for_remove_tx is called here, but will be overwritten
    }
}
```

The inflated `total_tx_size` is then used directly by `limit_size`:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee transactions
}
```

---

### Impact Explanation

- **Tx-pool DoS**: By repeatedly triggering the ancestor-eviction path, an attacker accumulates phantom size in `total_tx_size`. Once the phantom total exceeds `max_tx_pool_size`, `limit_size` begins evicting legitimate pending transactions submitted by other users, even though the pool has real capacity remaining.
- **Fee-rate distortion**: `estimate_fee_rate` iterates over real entries but the reported `total_tx_size` (exposed via the `get_pool_info` RPC) is wrong, misleading wallets and users about the true pool state.
- The bug also affects `total_tx_cycles`, which is used for the same size-limit enforcement.

**Impact: Medium** — mempool DoS affecting third-party transactions; no direct fund loss, but degrades liveness for all users of the node.

---

### Likelihood Explanation

The eviction branch in `check_and_record_ancestors` requires a transaction whose ancestor count exceeds `max_ancestors_count` (default 25) only because of cell-dep references (`cell_ref_parents`). An attacker can deliberately construct a chain of transactions where a new transaction references the outputs of many existing pool transactions as cell-deps, then submit the transaction that tips the ancestor count over the limit. This is a normal, permissionless tx-pool submission — no privileged access is required.

**Likelihood: Medium** — requires crafting a specific transaction graph, but is fully within the capability of any tx-pool submitter.

---

### Recommendation

Move the stat update to **after** all evictions have completed, reading the live `self.total_tx_size` at that point rather than using a pre-computed snapshot:

```rust
// Remove the pre-computation of total_tx_size / total_tx_cycles before evictions.
// After check_and_record_ancestors returns, compute the new totals from the
// already-updated self.total_tx_size:
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Now add only the new entry's contribution on top of the post-eviction total:
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, keep the pre-computation but subtract the sizes of all evicted entries before assigning:

```rust
let evicted_size: usize = evicts.iter().map(|e| e.size).sum();
let evicted_cycles: Cycle = evicts.iter().map(|e| e.cycles).sum();
self.total_tx_size = total_tx_size.saturating_sub(evicted_size);
self.total_tx_cycles = total_tx_cycles.saturating_sub(evicted_cycles);
```

---

### Proof of Concept

**Entry path**: Any unprivileged peer or RPC caller submitting transactions via `send_transaction` RPC → `TxPoolService::submit_entry` → `_submit_entry` → `pool_map.add_entry`.

**Steps**:

1. Submit `N = max_ancestors_count` transactions (`T1 … TN`) that each produce a cell output, so they all sit in the pool as cell-dep candidates.
2. Submit a new transaction `T_attack` that references all `N` outputs as cell-deps. Its `ancestors_count` = N+1 > `max_ancestors_count`, but `cell_ref_parents.len()` = N, so `ancestors_count − cell_ref_parents.len()` = 1 ≤ `max_ancestors_count`. The eviction branch fires.
3. `check_and_record_ancestors` evicts one or more of `T1…TN` (total evicted size = `S_evicted`). `update_stat_for_remove_tx` correctly decrements `self.total_tx_size` by `S_evicted`.
4. `add_entry` then assigns `self.total_tx_size = total_tx_size` (the pre-eviction snapshot + `T_attack.size`), inflating the counter by `S_evicted`.
5. Repeat steps 1–4 to accumulate phantom size until `total_tx_size > max_tx_pool_size`.
6. `limit_size` now continuously evicts legitimate transactions submitted by other users, even though the pool has real free space.

**Relevant code locations**: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** tx-pool/src/component/pool_map.rs (L710-758)
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
