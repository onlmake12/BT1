### Title
Incorrect `total_tx_size`/`total_tx_cycles` Accounting in `add_entry` Causes Inflated Pool-Size Counter and Spurious Transaction Evictions — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::add_entry` pre-computes the new `total_tx_size` / `total_tx_cycles` values **before** calling `check_and_record_ancestors`, which may itself evict pool entries (via `remove_entry_and_descendants` → `update_stat_for_remove_tx`). Those in-place decrements are then silently overwritten when `add_entry` writes the stale pre-eviction totals back. The result is that `total_tx_size` and `total_tx_cycles` are permanently inflated by the aggregate size/cycles of every entry evicted during ancestor-limit enforcement. Because `limit_size` drives its eviction loop off `total_tx_size`, the pool subsequently evicts additional legitimate transactions to compensate for the phantom inflation.

---

### Finding Description

In `PoolMap::add_entry` (`tx-pool/src/component/pool_map.rs`):

```rust
// Step 1 – snapshot new totals BEFORE any evictions
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Step 2 – may call remove_entry_and_descendants → update_stat_for_remove_tx,
//           which decrements self.total_tx_size / self.total_tx_cycles in-place
evicts = self.check_and_record_ancestors(&mut entry)?;

self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Step 3 – OVERWRITES the decrements from Step 2 with the stale Step-1 values
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` takes `&self` and returns `self.total_tx_size + entry.size` without touching the field. [1](#0-0) 

`check_and_record_ancestors` evicts `cell_ref_parents` when the ancestor count would exceed `max_ancestors_count`, calling `remove_entry_and_descendants` for each evicted entry. [2](#0-1) 

`remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx` correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in-place. [3](#0-2) 

The final write-back at lines 218–219 then overwrites those decrements with the stale snapshot from Step 1, leaving `total_tx_size` inflated by exactly `Σ size(evicted_i)` and `total_tx_cycles` inflated by `Σ cycles(evicted_i)`. [4](#0-3) 

`limit_size` drives its eviction loop with `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, so the phantom inflation causes it to evict additional legitimate transactions equal in aggregate size to the inflation. [5](#0-4) 

---

### Impact Explanation

Every time `add_entry` triggers a `cell_ref_parent` eviction, `total_tx_size` is permanently over-counted by the size of the evicted entries. The subsequent `limit_size` call (invoked after every submission and after every reorg) then expels an additional cohort of legitimate pending/proposed transactions whose aggregate serialized size equals the inflation. This is a tx-pool resource-accounting corruption: honest transactions are silently dropped from the mempool without any protocol-level error, reducing the effective pool capacity and degrading miner revenue and user experience. The pool's RPC-reported `total_tx_size` also becomes incorrect, misleading operators and fee-estimation logic. [6](#0-5) 

---

### Likelihood Explanation

The trigger condition — a new transaction whose ancestor count would exceed `max_ancestors_count` but whose `cell_ref_parent` count brings it back within limit — is reachable by any unprivileged tx-pool submitter. An attacker can deliberately construct a transaction chain near the ancestor limit and share a cell dep with existing pool entries, then submit a new transaction referencing that dep. No privileged access, key material, or majority hash-power is required. The attack is repeatable and each repetition compounds the inflation. [7](#0-6) 

---

### Recommendation

Move the `total_tx_size` / `total_tx_cycles` write-back to **after** `check_and_record_ancestors` completes, and base it on the **current** (post-eviction) `self.total_tx_size` rather than the pre-computed snapshot:

```rust
// Step 1 – validate that adding this entry won't overflow (but don't snapshot yet)
self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Step 2 – evictions happen here, correctly decrementing self.total_tx_size
evicts = self.check_and_record_ancestors(&mut entry)?;

self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Step 3 – apply the new entry's contribution to the already-correct post-eviction totals
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, compute the final totals after evictions by reading `self.total_tx_size` again at Step 3 instead of using the stale snapshot.

---

### Proof of Concept

1. Fill the pool with a chain `tx0 → tx1 → … → tx_{N-1}` where `N = max_ancestors_count - 1`. Each `tx_i` uses `cell_dep_A` as a cell dependency.
2. Submit `tx_N` which spends an output of `tx_{N-1}` **and** also uses `cell_dep_A`. Now `ancestors_count = N + 1 > max_ancestors_count`, but `cell_ref_parents = {tx0, …, tx_{N-1}}`, so the eviction branch is taken.
3. `check_and_record_ancestors` evicts, say, `tx_{N-1}` (lowest fee). `update_stat_for_remove_tx` decrements `self.total_tx_size` by `size(tx_{N-1})`.
4. `add_entry` then writes back `total_tx_size = old_total + size(tx_N)`, ignoring the decrement. `total_tx_size` is now `old_total + size(tx_N) + size(tx_{N-1})` instead of the correct `old_total + size(tx_N) - size(tx_{N-1})`.
5. `limit_size` sees `total_tx_size > max_tx_pool_size` (if the pool was near capacity) and evicts an additional transaction of size ≈ `size(tx_{N-1})` from the pool, even though the pool is actually within its limit.
6. Repeat from step 2 with a fresh `tx_N`; each iteration inflates `total_tx_size` further and expels more legitimate transactions.

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
