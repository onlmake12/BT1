### Title
Stale `total_tx_size`/`total_tx_cycles` Overwrite After In-Flight Eviction in `add_entry` Inflates Pool Accounting — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::add_entry` pre-computes new `total_tx_size` / `total_tx_cycles` values before calling `check_and_record_ancestors`, which can itself evict transactions (via `remove_entry_and_descendants`) and correctly decrement those same counters. After the evictions, `add_entry` unconditionally overwrites the counters with the stale pre-eviction values, permanently inflating them by the aggregate size/cycles of every evicted transaction. The inflated counters drive `limit_size`, which evicts additional legitimate transactions from the pool even though the pool is not actually full.

---

### Finding Description

In `tx-pool/src/component/pool_map.rs`, `add_entry` follows this sequence:

```
// Step 1 – snapshot new totals BEFORE any eviction
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // line 210-211

// Step 2 – may call remove_entry_and_descendants → update_stat_for_remove_tx
//           which CORRECTLY decrements self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;          // line 213

// Step 3 – OVERWRITES the correctly-decremented counters with the stale snapshot
self.total_tx_size  = total_tx_size;                            // line 218
self.total_tx_cycles = total_tx_cycles;                         // line 219
``` [1](#0-0) 

`updated_stat_for_add_tx` computes `self.total_tx_size + entry.size` at the moment of the call. If `check_and_record_ancestors` subsequently evicts one or more transactions, `update_stat_for_remove_tx` correctly subtracts their sizes from `self.total_tx_size`. But Step 3 then blindly restores the pre-eviction snapshot, erasing those subtractions. [2](#0-1) 

The eviction inside `check_and_record_ancestors` is triggered when a new transaction's ancestor count exceeds `max_ancestors_count` but can be reduced by removing cell-dep-referencing parents: [3](#0-2) 

After each such event, `total_tx_size` is inflated by exactly the sum of the evicted transactions' sizes. The code comment at line 732 already acknowledges a related known inaccuracy: *"cycles overflow is possible, currently obtaining cycles is not accurate"*. [4](#0-3) 

`limit_size` uses `self.pool_map.total_tx_size` directly to decide when to evict:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee-rate entries
}
``` [5](#0-4) 

Because `total_tx_size` is inflated, `limit_size` sees the pool as over-limit and evicts additional legitimate transactions that would otherwise remain.

---

### Impact Explanation

Every time the eviction branch inside `check_and_record_ancestors` fires, `total_tx_size` (and `total_tx_cycles`) is permanently inflated by the aggregate byte-size of the evicted transactions. `limit_size`, called immediately after `submit_entry`, then over-evicts legitimate pending/proposed transactions. Over repeated triggering:

- The pool is progressively under-utilized relative to `max_tx_pool_size`.
- Legitimate transactions are evicted and their submitters receive `Reject::Full` errors even though the pool has physical capacity.
- The `tx_pool_info` RPC reports an inflated `total_tx_size`, misleading operators and downstream tooling. [6](#0-5) 

---

### Likelihood Explanation

The eviction branch in `check_and_record_ancestors` requires a transaction whose ancestor count exceeds `max_ancestors_count` (default 1 000) but whose excess ancestors are all cell-dep-referencing parents. An unprivileged transaction sender reachable via the `send_transaction` RPC or P2P relay can craft such a chain. Because the default `max_ancestors_count` is 1 000, building a chain of that depth is expensive but not infeasible for a motivated attacker, and each successful trigger permanently inflates the counters. The bug also fires in non-adversarial conditions whenever organic transaction chains happen to hit the eviction path. [7](#0-6) 

---

### Recommendation

Move the counter update to **after** `check_and_record_ancestors` completes, so it reflects the post-eviction state:

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
    // Validate that adding this entry won't overflow, but do NOT commit yet.
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

    let evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));

    // Re-compute from current (post-eviction) self.total_tx_size.
    self.total_tx_size  = self.total_tx_size.saturating_add(entry.size);
    self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
    Ok((true, evicts))
}
```

Alternatively, call `recompute_total_stat()` after every `add_entry` that produced evictions, analogous to the fallback already present in `update_stat_for_remove_tx`. [8](#0-7) 

---

### Proof of Concept

1. Fill the pool with a chain of 999 transactions `T1 → T2 → … → T999`, where each `Ti` is a cell-dep parent of `T1000` (making `T1000`'s ancestor count = 1 000 via cell-dep references).
2. Submit `T1000`. `check_and_record_ancestors` detects `ancestors_count = 1001 > max_ancestors_count = 1000` but `ancestors_count - cell_ref_parents.len() ≤ 1000`, so it evicts, say, `T999` (size S).
3. After eviction, `self.total_tx_size` is correctly decremented by S. But Step 3 of `add_entry` overwrites it with the pre-eviction value, inflating `total_tx_size` by S.
4. `limit_size` is called; it sees `total_tx_size > max_tx_pool_size` and evicts one more legitimate transaction.
5. Repeat: each submission that triggers the eviction branch inflates `total_tx_size` by the evicted size, causing `limit_size` to cascade-evict additional legitimate transactions. [9](#0-8) [10](#0-9)

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

**File:** tx-pool/src/component/pool_map.rs (L595-601)
```rust
        let mut ancestors_count = ancestors.len() + 1;
        let mut evicted = Default::default();

        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
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

**File:** tx-pool/src/component/pool_map.rs (L698-708)
```rust
    fn recompute_total_stat(&self) -> Option<(usize, Cycle)> {
        self.entries.iter().try_fold(
            (0usize, 0 as Cycle),
            |(total_size, total_cycles), (_, entry)| {
                Some((
                    total_size.checked_add(entry.inner.size)?,
                    total_cycles.checked_add(entry.inner.cycles)?,
                ))
            },
        )
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

**File:** tx-pool/src/process.rs (L149-153)
```rust
                tx_pool.remove_conflict(&entry.proposal_short_id());
                tx_pool
                    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
                    .map_or(Ok(()), Err)?;

```
