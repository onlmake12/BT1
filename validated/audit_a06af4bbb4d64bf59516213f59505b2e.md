### Title
`total_tx_size` / `total_tx_cycles` Inflated When Evictions Occur During `add_entry` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new `total_tx_size` and `total_tx_cycles` values are computed as local variables **before** `check_and_record_ancestors` runs. If that function evicts transactions (via `remove_entry_and_descendants`), those removals decrement `self.total_tx_size` / `self.total_tx_cycles` in-place. But the final assignment at the end of `add_entry` overwrites those decremented values with the stale pre-eviction snapshot, permanently inflating the pool's size and cycle accounting.

---

### Finding Description

In `PoolMap::add_entry`:

```rust
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // (A) snapshot taken here

evicts = self.check_and_record_ancestors(&mut entry)?;          // (B) may evict entries
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
self.total_tx_size = total_tx_size;                             // (C) overwrites with stale value
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

At **(A)**, `updated_stat_for_add_tx` captures `self.total_tx_size + entry.size` into a local variable: [2](#0-1) 

At **(B)**, `check_and_record_ancestors` may call `remove_entry_and_descendants` when the new entry's ancestor count exceeds `max_ancestors_count` but can be reduced by evicting "cell-ref parents" (ancestors that reference the same cell dep). Each eviction calls `update_stat_for_remove_tx`, which decrements `self.total_tx_size` and `self.total_tx_cycles` in-place: [3](#0-2) [4](#0-3) 

At **(C)**, the local variable (computed before evictions) overwrites the correctly-decremented `self.total_tx_size`. The net result is:

```
final total_tx_size = original_total + entry.size
                    ≠ original_total − evicted_sizes + entry.size   (correct)
```

The pool's size counter is inflated by exactly the sum of the sizes of all evicted transactions.

---

### Impact Explanation

`total_tx_size` is the authoritative counter used in two critical places:

1. **`limit_size`** — evicts transactions while `total_tx_size > max_tx_pool_size`. An inflated counter causes legitimate, already-accepted transactions to be unnecessarily evicted. [5](#0-4) 

2. **`updated_stat_for_add_tx`** — rejects new submissions with `Reject::Full` when `total_tx_size` would overflow. An inflated counter causes premature rejection of valid transactions even when the pool has real capacity. [6](#0-5) 

The inflation persists indefinitely (until the pool is cleared or the node restarts), compounding with each subsequent eviction-triggering submission.

---

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged transaction submitter via the `send_transaction` RPC. The required conditions are:

- The new transaction's ancestor count exceeds `max_ancestors_count` (default 25).
- Some of those ancestors are "cell-ref parents" — transactions that reference the same cell dep as the new transaction's ancestors — so that evicting them brings the count within the limit.

An attacker can deliberately craft this scenario:
1. Submit a chain of 26+ transactions (tx₁ → tx₂ → … → tx₂₆), all using a shared cell dep `D`.
2. Submit tx₂₇ spending tx₂₆'s output and also referencing `D`.
3. tx₂₇ has 26 ancestors; `ancestors_count − cell_ref_parents.len()` ≤ 25, triggering eviction.
4. Each such submission inflates `total_tx_size` by the evicted transactions' sizes.

No privileged access, key material, or majority hashpower is required.

---

### Recommendation

Recompute the new totals **after** `check_and_record_ancestors` completes (and any evictions have already been applied to `self.total_tx_size`), rather than capturing a snapshot before evictions:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Recompute totals AFTER evictions have already updated self.total_tx_size
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, move the overflow check to a separate pre-flight guard that does not capture a stale snapshot, and always derive the final totals from the post-eviction state.

---

### Proof of Concept

1. Node starts with an empty pool (`total_tx_size = 0`).
2. Attacker submits 26 transactions (tx₁…tx₂₆) in a chain, each referencing cell dep `D`. Each tx is 200 bytes. Pool now holds 26 entries; `total_tx_size = 5200`.
3. Attacker submits tx₂₇ spending tx₂₆ and referencing `D`. `ancestors_count = 27 > 25`; `cell_ref_parents = {tx₁…tx₂₆}`; `27 − 26 = 1 ≤ 25` → eviction path triggers.
4. `updated_stat_for_add_tx` captures `total_tx_size_local = 5200 + 200 = 5400`.
5. `check_and_record_ancestors` evicts tx₁ (200 bytes): `self.total_tx_size = 5200 − 200 = 5000`.
6. tx₂₇ is inserted. `self.total_tx_size = total_tx_size_local = 5400` (overwrites 5000).
7. Actual pool holds 26 entries (tx₂…tx₂₇) = 5200 bytes, but `total_tx_size = 5400` — inflated by 200 bytes.
8. Repeating this pattern accumulates inflation, eventually causing `limit_size` to evict honest transactions or `updated_stat_for_add_tx` to reject valid submissions. [7](#0-6)

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

**File:** tx-pool/src/component/pool_map.rs (L588-639)
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
