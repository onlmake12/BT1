### Title
`total_tx_size` / `total_tx_cycles` Inflated by Stale Pre-Eviction Snapshot Overwrite in `add_entry` - (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the pool's aggregate size and cycle counters (`total_tx_size`, `total_tx_cycles`) are computed as a snapshot **before** any in-flight evictions occur. When `check_and_record_ancestors` evicts cell-dep-conflicting transactions, those evictions correctly decrement the live counters via `update_stat_for_remove_tx`. However, the function then unconditionally overwrites the live counters with the stale pre-eviction snapshot, permanently inflating both counters by the sum of all evicted entries' sizes and cycles. This is the direct CKB analog of the Kinetiq buffer-accounting bug: a counter is decremented during an intermediate operation, but then silently overwritten with a value that ignores those decrements.

---

### Finding Description

`add_entry` in `tx-pool/src/component/pool_map.rs` executes the following sequence:

```rust
// Step 1 – snapshot computed BEFORE evictions (lines 210-211)
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
//   = self.total_tx_size + entry.size  (stale snapshot)

// Step 2 – evictions happen HERE, mutating self.total_tx_size (line 213)
evicts = self.check_and_record_ancestors(&mut entry)?;
//   internally calls remove_entry_and_descendants → remove_entry
//   → update_stat_for_remove_tx, which does:
//       self.total_tx_size -= evicted.size   ← correct live decrement

// Step 3 – stale snapshot OVERWRITES the correctly-decremented live value (lines 218-219)
self.total_tx_size = total_tx_size;   // ← BUG: ignores evictions
self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` (lines 711-729) only reads `self.total_tx_size` at call time and adds `entry.size`. It does not account for any evictions that happen afterward. `check_and_record_ancestors` (lines 588-640) calls `remove_entry_and_descendants` (line 618) when the new transaction's cell-dep ancestors exceed `max_ancestors_count`, which in turn calls `remove_entry` (line 247), which calls `update_stat_for_remove_tx` (lines 733-758) to decrement `self.total_tx_size`. After all evictions complete, lines 218-219 blindly restore the pre-eviction snapshot, erasing every decrement that `update_stat_for_remove_tx` applied.

The correct final value should be:

```
total_tx_size = (original_total − Σ evicted_sizes) + new_entry_size
```

But the actual stored value is:

```
total_tx_size = original_total + new_entry_size   (evicted sizes NOT subtracted)
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

---

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size` to enforce the pool's memory cap:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee transactions
}
```

An inflated `total_tx_size` causes `limit_size` to believe the pool is over-capacity when it is not, triggering cascading evictions of legitimate, fee-paying transactions. Subsequent `send_transaction` RPC calls will receive `Reject::Full` even though the pool has real available space. The drift is permanent and cumulative: every insertion that triggers at least one cell-dep eviction adds `Σ evicted_sizes` to the phantom inflation. Over repeated submissions the pool's effective admission capacity shrinks to zero, constituting a **remote denial-of-service against the tx-pool** reachable by any unprivileged RPC caller. [5](#0-4) 

---

### Likelihood Explanation

The trigger condition — a new transaction whose cell dep is already consumed by existing pool transactions — is a normal operational scenario explicitly handled by the codebase (the `cell_ref_parents` eviction path). Any node operator running a public RPC endpoint is exposed. An attacker needs only to:

1. Observe which cell deps are referenced by pending pool transactions (visible via `get_raw_tx_pool`).
2. Craft a new transaction that also references those cell deps as inputs, forcing eviction of the existing transactions.
3. Repeat to accumulate phantom inflation until `total_tx_size` permanently exceeds `max_tx_pool_size`.

No privileged access, key material, or majority hashpower is required. [6](#0-5) 

---

### Recommendation

Move the stat snapshot computation to **after** `check_and_record_ancestors` completes, so it reflects the post-eviction pool state:

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
    // Pre-check for overflow only (do not capture snapshot yet)
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    // Compute final stats AFTER evictions have already mutated self.total_tx_size
    self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
    self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
    Ok((true, evicts))
}
``` [1](#0-0) 

---

### Proof of Concept

**Setup:** Pool with `max_tx_pool_size = 10_000` bytes, `max_ancestors_count = 3`. Pool already contains transactions `T_ref` (size 1000) and `T_dep` (size 1000) where `T_dep` uses `T_ref`'s output as a cell dep.

**Step 1 – Initial state:**
```
total_tx_size = 2000
```

**Step 2 – Attacker submits `T_new`** (size 500) that also consumes `T_ref`'s output as an input (not just a dep), causing `T_dep` to be a `cell_ref_parent` that must be evicted:

- `updated_stat_for_add_tx(500)` → snapshot = `2000 + 500 = 2500`
- `check_and_record_ancestors` evicts `T_dep` (size 1000):
  - `update_stat_for_remove_tx(1000)` → `self.total_tx_size = 2000 − 1000 = 1000`
- Lines 218-219 overwrite: `self.total_tx_size = 2500` ← **wrong**

**Correct value:** `1000 (T_ref) + 500 (T_new) = 1500`
**Actual stored value:** `2500`

**Step 3 – Repeat** with additional eviction-triggering transactions. Each iteration adds `evicted_size` phantom bytes. After enough iterations, `total_tx_size > max_tx_pool_size` permanently, and `limit_size` evicts every new submission with `Reject::Full`. [7](#0-6) [8](#0-7)

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
