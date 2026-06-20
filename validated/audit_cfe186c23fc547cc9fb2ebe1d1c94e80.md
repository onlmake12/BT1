### Title
`total_tx_size`/`total_tx_cycles` Inflated When Cell-Dep Evictions Occur During `add_entry` - (File: tx-pool/src/component/pool_map.rs)

---

### Summary

In `PoolMap::add_entry`, the updated pool-size statistics (`total_tx_size`, `total_tx_cycles`) are computed **before** `check_and_record_ancestors` is called. When that function evicts conflicting cell-dep ancestor transactions via `remove_entry_and_descendants`, each eviction correctly decrements `self.total_tx_size` and `self.total_tx_cycles` through `update_stat_for_remove_tx`. However, the final two lines of `add_entry` then **overwrite** those correctly-decremented values with the stale pre-eviction snapshot, permanently inflating the pool's reported size by the sum of all evicted transactions' sizes and cycles.

---

### Finding Description

`PoolMap::add_entry` computes the expected new totals on lines 210–211, before any evictions:

```rust
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
```

`updated_stat_for_add_tx` simply returns `(self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles)` — a snapshot of the pre-eviction state plus the new entry.

Then `check_and_record_ancestors` is called. When the incoming transaction's ancestor count exceeds `max_ancestors_count` but can be brought within the limit by evicting cell-dep-referencing ancestors, it calls `remove_entry_and_descendants`, which calls `remove_entry`, which calls `update_stat_for_remove_tx` — correctly decrementing `self.total_tx_size` and `self.total_tx_cycles` for each evicted entry.

But after all that, lines 218–219 unconditionally overwrite the now-correct in-place values:

```rust
self.total_tx_size = total_tx_size;   // stale: pre-eviction total + new entry
self.total_tx_cycles = total_tx_cycles; // stale: pre-eviction total + new entry
```

The evicted entries' sizes and cycles are never subtracted. The pool's accounting is permanently inflated by exactly the sum of the evicted entries' sizes/cycles.

The eviction path in `check_and_record_ancestors` is reached when:
1. `ancestors_count > max_ancestors_count`
2. `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`

A "cell ref parent" is a pool transaction that holds a cell as a `cell_dep` that the incoming transaction consumes as an input. This is a reachable, attacker-constructible condition via the public `send_transaction` RPC.

---

### Impact Explanation

After the inflation, `pool_map.total_tx_size` exceeds the true sum of all entries' sizes. Two downstream effects follow:

1. **Spurious eviction loop.** `TxPool::limit_size` loops while `self.pool_map.total_tx_size > self.config.max_tx_pool_size`. With an inflated counter, the loop evicts additional legitimate pending/proposed transactions that would otherwise have fit, permanently removing them from the pool.

2. **Spurious rejection of new submissions.** `updated_stat_for_add_tx` returns `Reject::Full` if the computed new total would overflow. Because the baseline is already inflated, subsequent honest transactions are rejected even though the pool has physical room.

Both effects persist until the node restarts or the pool is cleared, because there is no periodic reconciliation of `total_tx_size` against the actual entry set (the `recompute_total_stat` helper is only invoked on underflow, not on inflation).

---

### Likelihood Explanation

The trigger requires an attacker to:
1. Submit a transaction `P` that references some live cell `X` as a `cell_dep`.
2. Build a chain of ≥ `max_ancestors_count` pool transactions that descend from `P` (default limit is 25).
3. Submit a new transaction `N` that **consumes** cell `X` as an input.

When `N` arrives, `get_tx_ancenstors` identifies `P` (and its descendants) as `cell_ref_parents`. Because removing them brings the ancestor count within the limit, `check_and_record_ancestors` evicts them — triggering the accounting bug. All steps are reachable through the public `send_transaction` RPC endpoint with no privileged access. The attacker must pay transaction fees, but the cost is bounded by the number of transactions needed to exceed `max_ancestors_count`.

---

### Recommendation

Move the stat computation to **after** `check_and_record_ancestors` returns, so that any in-place decrements from evictions are already reflected:

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
    // Validate capacity headroom before any mutation
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

    let evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));

    // Compute totals AFTER evictions have already decremented self.total_tx_*
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    self.total_tx_size = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;
    Ok((true, evicts))
}
```

Alternatively, accumulate the evicted sizes inside `check_and_record_ancestors` and subtract them from `total_tx_size` before the final assignment.

---

### Proof of Concept

**Setup:** `max_ancestors_count = 25`, `max_tx_pool_size = 10 MB`.

1. Submit live cell `X` on-chain (or use an existing UTXO).
2. Submit pool transaction `P` with `cell_dep = X`. Size = 500 B.
3. Submit 25 pool transactions `C1 … C25`, each spending the output of the previous, forming a chain rooted at `P`. Each is 500 B.
4. Submit transaction `N` that spends cell `X` as an input. `N`'s ancestor count = 26 (P + C1…C25), exceeding the limit of 25. `cell_ref_parents = {P}`. Since `26 - 1 = 25 <= 25`, the eviction branch fires.
5. `remove_entry_and_descendants(P)` removes P and all 25 descendants (26 entries × 500 B = 13 000 B). `update_stat_for_remove_tx` is called 26 times, correctly decrementing `self.total_tx_size` by 13 000 B.
6. Lines 218–219 then set `self.total_tx_size = (original_total + 500)` — the pre-eviction snapshot plus `N`'s size — ignoring the 13 000 B subtraction.
7. `total_tx_size` is now inflated by 13 000 B. Repeating steps 2–6 accumulates inflation without bound.
8. Once `total_tx_size` exceeds `max_tx_pool_size`, `limit_size` begins evicting honest transactions, and new honest submissions receive `Reject::Full`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** tx-pool/src/pool.rs (L292-328)
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
```
