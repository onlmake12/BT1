### Title
`total_tx_size`/`total_tx_cycles` Not Updated After Eviction in `add_entry` — (`tx-pool/src/component/pool_map.rs`)

### Summary

In `PoolMap::add_entry`, the pool's aggregate accounting fields `total_tx_size` and `total_tx_cycles` are computed **before** any eviction occurs, but written back **after** eviction, silently overwriting the correctly decremented values. This mirrors the external report's pattern: a partial operation (eviction during insertion) leaves a critical accounting field stale, causing the pool to believe it is larger than it actually is, which in turn triggers unnecessary eviction of legitimate transactions.

### Finding Description

`add_entry` in `tx-pool/src/component/pool_map.rs` follows this sequence:

1. **Pre-eviction snapshot** — `updated_stat_for_add_tx` computes `total_tx_size = self.total_tx_size + entry.size` and stores the result in a **local variable**.
2. **Eviction** — `check_and_record_ancestors` may call `remove_entry_and_descendants`, which calls `remove_entry`, which calls `update_stat_for_remove_tx`, **mutating `self.total_tx_size` in place** (decrementing it by the evicted entries' sizes).
3. **Stale overwrite** — `self.total_tx_size = total_tx_size` and `self.total_tx_cycles = total_tx_cycles` write the **pre-eviction snapshot** back, discarding the correct decrements. [1](#0-0) 

The eviction path inside `check_and_record_ancestors` is triggered when the incoming transaction has more ancestors than `max_ancestors_count`, but some of those ancestors are `cell_ref_parents` (pool transactions that hold a cell dep whose out-point is being consumed as an input by the new transaction). Those conflicting ancestors are removed via `remove_entry_and_descendants`. [2](#0-1) 

Each removal correctly decrements `self.total_tx_size` through `update_stat_for_remove_tx`: [3](#0-2) 

But the final assignment at lines 218–219 overwrites those decrements with the stale snapshot, inflating `total_tx_size` by exactly the sum of the evicted entries' sizes.

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size` to decide whether to evict further transactions from the pool: [4](#0-3) 

When `total_tx_size` is inflated, `limit_size` will evict additional legitimate pending/proposed transactions until the (incorrect) counter falls below `max_tx_pool_size`. The evicted transactions are broadcast as rejected to callbacks and removed from the pool, even though the pool was not actually over its limit. This constitutes a **tx-pool denial-of-service**: an attacker can reliably cause honest users' transactions to be expelled from the mempool without those transactions ever being invalid.

### Likelihood Explanation

The trigger condition is reachable by any unprivileged tx-pool submitter:

1. Attacker creates a chain of ≥ 25 pool transactions (T₁ … T₂₅), some of which carry a cell dep pointing to an on-chain cell **C** they control.
2. Attacker submits T_new with two inputs: one spending cell **C** (on-chain), one spending an output of T₂₅ (pool). This gives T_new 26 ancestors, exceeding `max_ancestors_count = 25`.
3. Because `ancestors_count − cell_ref_parents.len() ≤ 25`, the eviction branch fires, removing the cell_ref_parent transactions.
4. `total_tx_size` is inflated by the evicted transactions' sizes; `limit_size` then expels honest transactions.

The attacker only needs enough CKB to fund the chain and cell C — no privileged access, no majority hashpower, no social engineering.

### Recommendation

Compute the new totals **after** any eviction, not before. Replace the pre-eviction snapshot pattern with a post-eviction increment:

```rust
// After check_and_record_ancestors (eviction complete), add the new entry's contribution.
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, remove the local-variable snapshot entirely and call `updated_stat_for_add_tx` only after `check_and_record_ancestors` returns, so the base values already reflect any evictions.

### Proof of Concept

```
Initial state: total_tx_size = 100 (pool near limit of 110)

Step 1: add_entry(new_tx, size=10)
  updated_stat_for_add_tx → local total_tx_size = 110

Step 2: check_and_record_ancestors evicts 3 cell_ref_parents (sizes 15+15+15=45)
  update_stat_for_remove_tx called 3×: self.total_tx_size = 100 - 45 = 55

Step 3: self.total_tx_size = 110  ← stale overwrite

Correct value: 55 + 10 = 65  (well below limit of 110)
Actual value:  110             (at the limit)

limit_size() fires, evicts honest transactions until total_tx_size ≤ 110.
``` [5](#0-4) [6](#0-5)

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

**File:** tx-pool/src/pool.rs (L298-328)
```rust
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
```
