### Title
`total_tx_size` / `total_tx_cycles` Inflated When Ancestor-Eviction Occurs During `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new transaction's contribution to `total_tx_size` and `total_tx_cycles` is computed **before** any ancestor-eviction side-effects occur, stored in local variables, and then unconditionally written back **after** eviction. Any decrements applied to `self.total_tx_size` / `self.total_tx_cycles` by the eviction path are silently overwritten, leaving the pool's accounting permanently inflated. An unprivileged transaction sender can exploit this to make the pool believe it is full, causing legitimate transactions to be rejected or unnecessarily evicted.

---

### Finding Description

`add_entry` in `pool_map.rs` follows this sequence:

```
// Step 1 – snapshot totals + new tx, stored in LOCAL variables
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // line 210-211

// Step 2 – may evict transactions; each eviction calls
//           update_stat_for_remove_tx → DECREMENTS self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;          // line 213

// ... insert, link, track ...

// Step 3 – OVERWRITE self fields with the stale local snapshot from Step 1
self.total_tx_size  = total_tx_size;                            // line 218
self.total_tx_cycles = total_tx_cycles;                         // line 219
```

`updated_stat_for_add_tx` captures `self.total_tx_size + new_size` into a local. `check_and_record_ancestors` can then call `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size`. But the final assignment at lines 218-219 restores the pre-eviction snapshot, erasing those decrements.

**Net effect per eviction cycle:**

```
final total_tx_size = original_total + new_tx_size
                      (evicted sizes are NOT subtracted)
```

The eviction path in `check_and_record_ancestors` is triggered when:
1. `ancestors_count > max_ancestors_count`, AND
2. `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`

A transaction sender can craft transactions that satisfy both conditions (a new tx whose inputs consume outputs that existing pool transactions reference as cell-deps, creating `cell_ref_parents`), triggering eviction and inflating the counters on every such submission.

---

### Impact Explanation

`limit_size` in `pool.rs` (line 298) drives eviction decisions directly from `pool_map.total_tx_size`:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee transactions
}
```

With inflated `total_tx_size`:

- The pool evicts legitimate, higher-fee transactions unnecessarily.
- Subsequent submissions are rejected with `Reject::Full` even though actual pool occupancy is below `max_tx_pool_size`.
- `tx_pool_info` RPC returns a falsely elevated `total_tx_size`, misleading wallets and fee estimators.
- Repeated triggering accumulates inflation monotonically until the pool is effectively frozen for new submissions — a targeted, low-cost DoS against the mempool.

---

### Likelihood Explanation

The trigger requires a transaction with `cell_ref_parents` that push ancestor count just over `max_ancestors_count`. This is a crafted but realistic transaction graph reachable by any unprivileged RPC or P2P transaction sender. No privileged access, key material, or majority hashpower is required. The attacker pays only normal transaction fees for the triggering transactions.

---

### Recommendation

Compute the new totals **after** all evictions complete, not before. Replace the pre-eviction snapshot pattern with a post-eviction delta:

```rust
// After check_and_record_ancestors returns, recompute from current self fields:
self.total_tx_size  = self.total_tx_size
    .checked_add(entry.size)
    .expect("size overflow after eviction");
self.total_tx_cycles = self.total_tx_cycles
    .checked_add(entry.cycles)
    .expect("cycles overflow after eviction");
```

Or, equivalently, move `updated_stat_for_add_tx` to after `check_and_record_ancestors` so it reads the already-decremented `self.total_tx_size`.

---

### Proof of Concept

**Setup:**
- `max_ancestors_count = 25`
- Pool contains 25 transactions T1…T25 forming a chain; T25 also references output O_base as a cell-dep.
- O_base is a live chain cell.

**Attack step:**
1. Submit T_attack: inputs = [O_base] (consuming it), cell-deps = [T1…T25 outputs].
   - `ancestors_count` = 26 (T1…T25 + T_attack) > 25.
   - `cell_ref_parents` = {T25} (T25 references O_base as cell-dep; T_attack consumes O_base).
   - `26 - 1 = 25 <= 25` → eviction branch taken.
   - T25 (and its descendants, none here) is evicted: `update_stat_for_remove_tx(T25.size, T25.cycles)` decrements `self.total_tx_size`.
   - T_attack is inserted.
   - Lines 218-219 restore `total_tx_size = original_total + T_attack.size` — T25's size is added back.

2. Repeat with fresh transactions to accumulate inflation.

3. Once `total_tx_size` exceeds `max_tx_pool_size`, `limit_size` evicts all pending transactions and the pool rejects every new submission with `Reject::Full`.

**Relevant lines:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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
