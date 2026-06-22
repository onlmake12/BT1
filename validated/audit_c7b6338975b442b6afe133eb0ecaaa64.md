### Title
Stale `total_tx_size`/`total_tx_cycles` Accounting After Ancestor-Eviction in `add_entry` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new totals for `total_tx_size` and `total_tx_cycles` are computed **before** a potential in-pool eviction occurs inside `check_and_record_ancestors`. When evictions do happen, they correctly decrement `self.total_tx_size`/`self.total_tx_cycles` via `update_stat_for_remove_tx`, but those decrements are immediately **overwritten** by the stale pre-eviction values. The result is that `total_tx_size` and `total_tx_cycles` are permanently inflated by the sizes/cycles of every evicted transaction, causing the pool to believe it is fuller than it actually is and eventually rejecting all new transactions.

---

### Finding Description

`PoolMap::add_entry` follows this sequence:

```
// Step 1 – snapshot totals BEFORE any eviction
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
//   = self.total_tx_size + entry.size

// Step 2 – may call remove_entry_and_descendants → update_stat_for_remove_tx
//          which MUTATES self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

// Step 3 – OVERWRITES the correctly-updated self.total_tx_size with the stale snapshot
self.total_tx_size  = total_tx_size;   // ← stale: ignores eviction subtractions
self.total_tx_cycles = total_tx_cycles; // ← stale: ignores eviction subtractions
``` [1](#0-0) 

The eviction path inside `check_and_record_ancestors` is triggered when a new transaction's ancestor count exceeds `max_ancestors_count` **and** some of those ancestors are reachable only via cell-dep references (`cell_ref_parents`). In that case, `remove_entry_and_descendants` is called for each candidate, which internally calls `update_stat_for_remove_tx` and correctly decrements `self.total_tx_size`: [2](#0-1) 

`update_stat_for_remove_tx` performs a checked subtraction and falls back to a full recompute only on underflow: [3](#0-2) 

Because the overwrite at lines 218–219 happens **after** those subtractions, the evicted transactions' sizes/cycles are never reflected in the final totals. Each successful eviction-triggering `add_entry` call inflates `total_tx_size` by the cumulative size of all evicted entries.

---

### Impact Explanation

`total_tx_size` and `total_tx_cycles` are the authoritative pool-fullness counters. They are:

1. **Checked on every new submission** — `updated_stat_for_add_tx` returns `Reject::Full` if adding the new entry would overflow, so an inflated counter causes premature rejection of legitimate transactions. [4](#0-3) 

2. **Exposed via RPC** — `tx_pool_info` reports `total_tx_size` and `total_tx_cycles` directly, misleading operators and downstream tooling. [5](#0-4) 

3. **Used for pool-size enforcement** — `limit_size` evicts low-fee transactions when `total_tx_size > max_tx_pool_size`, so an inflated counter causes unnecessary eviction of valid transactions.

The net effect is a **transaction-pool DoS**: by repeatedly triggering the eviction path, an attacker can inflate `total_tx_size` until it exceeds `max_tx_pool_size`, after which every new transaction submission is rejected with `Reject::Full` regardless of actual pool occupancy. The pool never self-corrects because `recompute_total_stat` is only invoked on underflow, not on inflation. [6](#0-5) 

---

### Likelihood Explanation

**Medium.** The eviction path requires a new transaction whose ancestor count (including cell-dep ancestors) exceeds `max_ancestors_count` (default 25), with at least one ancestor reachable only via a cell dep. An attacker who controls a modest number of UTXOs can:

1. Build a chain of up to `max_ancestors_count − 1` pending transactions, where one transaction produces an output used as a **cell dep** by later transactions.
2. Submit a new transaction that both (a) spends an output deep in the chain and (b) references the cell-dep-producing transaction, pushing the ancestor count over the limit.
3. The cell-dep ancestor is evicted; `total_tx_size` is inflated by its size.
4. Repeat with fresh UTXOs.

Each round costs only transaction fees. No privileged access, no majority hashpower, and no Sybil attack is required — any RPC caller or P2P transaction relayer can trigger this path.

---

### Recommendation

Compute the new totals **after** `check_and_record_ancestors` returns, not before, so that any eviction-driven decrements are already reflected in `self.total_tx_size`/`self.total_tx_cycles`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute and apply totals AFTER evictions have already updated self.total_tx_*
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size  = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, drop the local snapshot entirely and apply the addition in-place after all mutations are complete.

---

### Proof of Concept

**Setup:**
- `max_ancestors_count = 25`
- Pool is empty; `total_tx_size = 0`

**Steps:**

1. Submit `tx_root` that produces output `O` (used later as a cell dep).
2. Submit a chain `tx_1 → tx_2 → … → tx_24` spending `tx_root`'s output, each ~200 bytes. Pool now has 25 entries; `total_tx_size ≈ 5000`.
3. Submit `tx_attack` that:
   - Spends `tx_24`'s output (making `tx_root … tx_24` its input-ancestors, count = 25)
   - Also declares `tx_root`'s output `O` as a **cell dep** (making `tx_root` a `cell_ref_parent`)
   - Ancestor count = 26 > 25, so eviction is triggered for `tx_root` (size ~200 bytes).

**What happens inside `add_entry`:**

```
// Before check_and_record_ancestors:
total_tx_size (local) = 5000 + size(tx_attack) ≈ 5200

// Inside check_and_record_ancestors → remove_entry_and_descendants(tx_root):
self.total_tx_size = 5000 − 200 = 4800   ← correct intermediate value

// After check_and_record_ancestors, line 218:
self.total_tx_size = 5200   ← stale snapshot overwrites; tx_root's 200 bytes are "ghost" size
```

4. Actual pool entries: `tx_1 … tx_24, tx_attack` (25 entries, ~5000 bytes actual).  
   Reported `total_tx_size`: **5200** — inflated by 200 bytes.

5. Repeat step 3 with fresh UTXOs. Each iteration adds ~200 bytes of phantom size.  
   After ~`(max_tx_pool_size / 200)` iterations, `total_tx_size` exceeds `max_tx_pool_size` and all subsequent submissions are rejected with `Reject::Full`. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** tx-pool/src/service.rs (L1086-1097)
```rust
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
