The code confirms the bug exactly as described. The call chain is verified:

- `add_entry` L210-211: captures stale snapshot into locals before any mutations [1](#0-0) 
- `check_and_record_ancestors` L618: calls `remove_entry_and_descendants` during eviction [2](#0-1) 
- `remove_entry_and_descendants` L263: calls `remove_entry` for each evicted tx [3](#0-2) 
- `remove_entry` L247: calls `update_stat_for_remove_tx`, correctly decrementing `self.total_tx_size` [4](#0-3) 
- `add_entry` L218-219: overwrites the correctly-decremented values with the stale locals [5](#0-4) 

---

Audit Report

## Title
Stale `total_tx_size`/`total_tx_cycles` Snapshot Overwrites Post-Eviction State in `add_entry` — (File: tx-pool/src/component/pool_map.rs)

## Summary
In `PoolMap::add_entry`, the new totals are pre-computed into local variables before `check_and_record_ancestors` runs. When that function evicts entries via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, `self.total_tx_size` and `self.total_tx_cycles` are correctly decremented in place. However, the final two lines of `add_entry` unconditionally overwrite those fields with the stale pre-eviction locals, permanently inflating both counters by the aggregate size/cycles of every evicted entry. An unprivileged submitter can repeat this to push `total_tx_size` past `max_tx_pool_size`, causing the node to reject all further transaction submissions.

## Finding Description
`add_entry` executes in this order:

```rust
// L210-211: snapshot taken BEFORE any evictions
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// L213: may evict entries; each eviction calls update_stat_for_remove_tx,
//        which correctly decrements self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

// ... insert new entry ...

// L218-219: stale pre-eviction snapshot overwrites the correct post-eviction value
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` computes `self.total_tx_size + entry.size` at call time and returns it as a plain integer — it does not hold a reference to `self`. [6](#0-5) 

`check_and_record_ancestors` reaches the eviction branch when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. It calls `remove_entry_and_descendants` in a loop. [7](#0-6) 

`remove_entry_and_descendants` calls `remove_entry` for each removed id, and `remove_entry` calls `update_stat_for_remove_tx`, which writes the decremented value directly into `self.total_tx_size`. [8](#0-7) 

The final assignment at L218-219 then discards that correct value and replaces it with the stale snapshot. Concrete arithmetic: initial=100, new entry size=10, evicted entry size=20 → local=110, post-eviction self=80, after overwrite self=110 (correct: 90). Inflation per cycle = sum of evicted entry sizes. [5](#0-4) 

## Impact Explanation
`total_tx_size` is the sole guard in `limit_size` (`while self.pool_map.total_tx_size > self.config.max_tx_pool_size`) and in `updated_stat_for_add_tx` (returns `Reject::Full` on overflow/excess). [9](#0-8) 

Once the inflated counter exceeds `max_tx_pool_size` (default 180 MB), every subsequent `add_entry` call returns `Reject::Full` regardless of actual pool occupancy. The node stops accepting any new transactions from any peer or RPC caller until restarted. This is a **node-level tx-pool DoS**, matching the allowed High impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* Applying the same attack to multiple nodes simultaneously escalates to network-wide congestion.

## Likelihood Explanation
No special privilege is required — only the ability to submit transactions via RPC or P2P relay. The attacker needs enough CKB to fund a ~126-deep transaction chain, which is a low economic bar. The `cell_ref_parent` condition (an ancestor referenced as a cell dep) is deliberately constructible. Each attack round inflates the counter by the aggregate size of the evicted chain (~126 × ~few hundred bytes). After O(max_tx_pool_size / evicted_chain_size) rounds the counter exceeds the limit. The attack is repeatable with fresh UTXOs and requires no timing precision or Sybil capability.

## Recommendation
Move the stat commitment to after all mutations, computing the delta from the already-updated `self.total_tx_size`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute from post-eviction self.total_tx_size
self.total_tx_size = self.total_tx_size
    .checked_add(entry.size)
    .ok_or_else(|| Reject::Full(...))?;
self.total_tx_cycles = self.total_tx_cycles
    .checked_add(entry.cycles)
    .ok_or_else(|| Reject::Full(...))?;
```

Alternatively, call `updated_stat_for_add_tx` after `check_and_record_ancestors` returns so the snapshot reflects the post-eviction state. Either approach eliminates the stale-snapshot overwrite.

## Proof of Concept
1. Connect to a CKB node RPC with a funded wallet.
2. Submit a chain A₁ → A₂ → … → A₁₂₆ (each spending the previous output).
3. Craft transaction B: input spends A₁₂₆'s output; cell dep references A₁'s output (making A₁ a `cell_ref_parent`).
4. Submit B. The pool evicts A₁ and descendants (A₂…A₁₂₆) to satisfy the ancestor limit, then inserts B. `total_tx_size` is inflated by the sum of sizes of A₁…A₁₂₆.
5. Repeat steps 2–4 with fresh UTXOs.
6. After enough iterations, observe that `send_transaction` returns `Reject::Full` even though the pool contains only a handful of entries (verifiable via `get_pool_info` which exposes `total_tx_size` directly). [10](#0-9)

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

**File:** tx-pool/src/component/pool_map.rs (L261-264)
```rust
        removed_ids
            .iter()
            .filter_map(|id| self.remove_entry(id))
            .collect()
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

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```
