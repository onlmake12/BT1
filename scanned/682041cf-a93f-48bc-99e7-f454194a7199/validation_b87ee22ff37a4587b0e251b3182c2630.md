### Title
`total_tx_size`/`total_tx_cycles` Invariant Broken by Ancestor-Eviction During `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

### Summary

`PoolMap` maintains `total_tx_size` and `total_tx_cycles` as running accumulators that gate pool admission and eviction. In `add_entry`, the new totals are computed **before** `check_and_record_ancestors` runs, but written back **after** it — silently overwriting any decrements made by evictions that occurred inside `check_and_record_ancestors`. This permanently inflates `total_tx_size` and `total_tx_cycles` relative to the actual pool contents, causing `limit_size` to over-evict legitimate transactions.

### Finding Description

`PoolMap::add_entry` computes the prospective new totals first, then calls `check_and_record_ancestors`, then unconditionally writes the pre-computed totals back:

```rust
// tx-pool/src/component/pool_map.rs  add_entry()
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // ← snapshot taken here

evicts = self.check_and_record_ancestors(&mut entry)?;          // ← may call remove_entry_and_descendants
                                                                //   which calls update_stat_for_remove_tx
                                                                //   and DECREMENTS self.total_tx_size / self.total_tx_cycles

self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
self.total_tx_size = total_tx_size;    // ← OVERWRITES the decremented value
self.total_tx_cycles = total_tx_cycles; // ← OVERWRITES the decremented value
``` [1](#0-0) 

Inside `check_and_record_ancestors`, when the incoming transaction's ancestor count (including `cell_ref_parents`) exceeds `max_ancestors_count`, the code evicts the lowest-fee `cell_ref_parent` entries via `remove_entry_and_descendants`:

```rust
let removed = self.remove_entry_and_descendants(next_id);
// remove_entry → update_stat_for_remove_tx → self.total_tx_size -= evicted.size
``` [2](#0-1) 

`remove_entry` calls `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size`: [3](#0-2) 

But those decrements are immediately discarded when `add_entry` writes back the stale pre-computed `total_tx_size` at lines 218–219. The result: `total_tx_size` is inflated by the size of every evicted entry, and this inflation is permanent (no recomputation is triggered on the inflation path — `recompute_total_stat` is only called on underflow). [4](#0-3) 

`limit_size` then uses the inflated `total_tx_size` to decide how many transactions to evict:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict entries ...
}
``` [5](#0-4) 

### Impact Explanation

Each time the ancestor-eviction path fires, `total_tx_size` grows by the size of the evicted entries without a corresponding real increase in pool contents. After repeated triggering, `total_tx_size` can far exceed the actual pool byte count. `limit_size` then aggressively evicts legitimate pending/proposed transactions to bring the phantom total below `max_tx_pool_size`, potentially emptying the pool entirely. This is a **tx-pool denial-of-service**: honest users' transactions are expelled and cannot be re-admitted while the attacker keeps re-triggering the inflation.

**Impact: High** — persistent pool state corruption leading to denial of service for all tx submitters on the node.

### Likelihood Explanation

The `cell_ref_parent` eviction path requires:
1. A chain of ≥ `max_ancestors_count − 1` transactions in the pool (default 25).
2. At least one in-pool transaction that uses one of those chain outputs as a **cell dep**.
3. A new transaction that **consumes** that same output as an input, pushing the ancestor count over the limit.

An unprivileged RPC caller (`send_transaction`) can construct this pattern with normal CKB transactions. The attacker needs enough CKB to fund ~26 transactions per trigger cycle, which is a low barrier. The attack can be repeated to accumulate inflation.

**Likelihood: Medium** — requires deliberate construction but no privileged access.

### Recommendation

Move the `total_tx_size`/`total_tx_cycles` write to **after** `check_and_record_ancestors` completes, using the **current** (post-eviction) values of `self.total_tx_size` and `self.total_tx_cycles` as the base, not the pre-computed snapshot:

```rust
// Instead of:
let (total_tx_size, total_tx_cycles) = self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
evicts = self.check_and_record_ancestors(&mut entry)?;
// ... insert ...
self.total_tx_size = total_tx_size;   // stale
self.total_tx_cycles = total_tx_cycles; // stale

// Do:
evicts = self.check_and_record_ancestors(&mut entry)?;
// Now self.total_tx_size reflects evictions; add the new entry's contribution:
let (total_tx_size, total_tx_cycles) = self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
// ... insert ...
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, call `recompute_total_stat()` after every `add_entry` that returns non-empty evicts, or assert the invariant `total_tx_size == entries.iter().sum(size)` in debug builds.

### Proof of Concept

**Setup:**
- `max_ancestors_count = 25`, `max_tx_pool_size = 20 MB`
- Attacker controls a funded CKB address.

**Steps:**
1. Submit `tx_0` (creates output `cell_0`).
2. Submit `tx_1 … tx_24` forming a chain: each spends the previous tx's output.
3. Submit `tx_dep` that uses `tx_24`'s output as a **cell dep** (not consuming it). `tx_dep` is now a `cell_ref_parent` for any future tx that spends `tx_24`'s output.
4. Submit `tx_25` that spends `tx_24`'s output as an **input**.
   - `get_tx_ancenstors` returns ancestors = {`tx_0`…`tx_24`} (25) + `tx_dep` as `cell_ref_parent` → `ancestors_count = 26 > 25`.
   - `check_and_record_ancestors` evicts `tx_dep` via `remove_entry_and_descendants` → `self.total_tx_size -= size(tx_dep)`.
   - `add_entry` then writes back `total_tx_size = old_total + size(tx_25)` (ignoring the subtraction).
   - **Net inflation: `+size(tx_dep)`**.
5. Repeat steps 3–4 with fresh outputs. Each iteration inflates `total_tx_size` by `size(tx_dep)`.
6. Once `total_tx_size` (phantom) exceeds `max_tx_pool_size`, `limit_size` evicts all real transactions from the pool, even though the actual pool byte count is well within limits. [6](#0-5) [7](#0-6)

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
