### Title
Stale Pre-Computed Pool Accounting Overwrites Eviction-Adjusted Totals, Inflating `total_tx_size`/`total_tx_cycles` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the pool's aggregate size and cycle counters (`total_tx_size`, `total_tx_cycles`) are pre-computed **before** `check_and_record_ancestors` runs. That function can evict existing entries via `remove_entry_and_descendants`, which correctly decrements the counters through `update_stat_for_remove_tx`. However, `add_entry` then **overwrites** those correctly-decremented counters with the stale pre-computed values, silently discarding the eviction adjustments. The result is that `total_tx_size` and `total_tx_cycles` become permanently inflated by the aggregate size/cycles of every evicted entry, causing the pool to report itself as fuller than it is and eventually rejecting all new transactions with `Reject::Full`.

---

### Finding Description

In `tx-pool/src/component/pool_map.rs`, `add_entry` follows this sequence:

```rust
// Step 1: pre-compute new totals BEFORE any evictions
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Step 2: may evict entries via remove_entry_and_descendants → remove_entry
//         → update_stat_for_remove_tx, which DECREMENTS self.total_tx_size/cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

// ... insert new entry ...

// Step 3: OVERWRITES the correctly-decremented counters with the stale pre-computed values
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

The pre-computed `total_tx_size` equals `old_self.total_tx_size + entry.size`. If `check_and_record_ancestors` evicts entries whose aggregate size is `S_evicted`, `update_stat_for_remove_tx` correctly sets `self.total_tx_size = old_self.total_tx_size - S_evicted`. But Step 3 then writes `old_self.total_tx_size + entry.size`, discarding the `- S_evicted` adjustment. The final counter is inflated by exactly `S_evicted`. [2](#0-1) 

The eviction path inside `check_and_record_ancestors` is triggered when a new transaction's ancestor count exceeds `max_ancestors_count` but can be reduced by evicting cell-dep-referencing ancestor transactions: [3](#0-2) 

Each evicted entry goes through `remove_entry` → `update_stat_for_remove_tx`, which modifies `self.total_tx_size` and `self.total_tx_cycles` in place: [4](#0-3) 

The code even contains a `FIXME` comment acknowledging the eviction scenario is real and that rollback is not handled: [5](#0-4) 

The pool-full check in `updated_stat_for_add_tx` uses `self.total_tx_size` and `self.total_tx_cycles` directly: [6](#0-5) 

Once inflated, these counters are never corrected downward (the underflow recovery in `update_stat_for_remove_tx` only recomputes from actual entries, but the inflation is in the *global* counter, not in any individual entry). Every subsequent `add_entry` call reads the inflated baseline, so the inflation is permanent and cumulative across multiple exploit iterations.

---

### Impact Explanation

`total_tx_size` and `total_tx_cycles` are the sole gate for pool admission. Once inflated past the configured `max_tx_pool_size` or `max_block_cycles`, every subsequent `add_entry` call returns `Reject::Full`, permanently blocking all new transaction submissions to the node's tx-pool. This is a **node-level DoS on transaction ingestion**: the node continues to operate and sync blocks, but its tx-pool becomes permanently closed to new transactions, preventing it from participating in transaction relay and block assembly.

---

### Likelihood Explanation

The eviction path requires an attacker to submit a sequence of transactions such that:
1. A set of transactions form a cell-dep ancestor chain of length ≥ `max_ancestors_count` (default 25).
2. A new transaction references one of those ancestors as a cell dep, pushing the ancestor count over the limit.
3. The condition `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` is satisfied, triggering eviction rather than outright rejection.

This is achievable by an unprivileged tx-pool submitter (via RPC `send_transaction` or P2P relay) with no special privileges. The attacker pays only the minimum fee for each transaction. The exploit can be repeated across multiple `add_entry` calls to accumulate inflation until the pool is permanently closed.

---

### Recommendation

Move the counter update to **after** `check_and_record_ancestors` completes, using the actual post-eviction state of `self.total_tx_size` and `self.total_tx_cycles` rather than the pre-computed snapshot:

```rust
// Remove the pre-computation before check_and_record_ancestors.
// After all evictions and the new entry is inserted, update counters atomically:
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Now compute and apply the delta correctly on the post-eviction baseline:
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, validate pool capacity limits after evictions complete, not before.

---

### Proof of Concept

1. Configure a node with `max_ancestors_count = 25` (default).
2. Submit 25 transactions forming a linear chain: `tx_0 → tx_1 → … → tx_24`, where each `tx_i` spends an output of `tx_{i-1}`. All 25 are accepted; `total_tx_size` = sum of their sizes.
3. Submit `tx_A` that spends an output of `tx_0` **and** uses `tx_12` as a cell dep. `tx_A`'s ancestor set includes `tx_0`…`tx_24` (26 ancestors > 25 limit). The cell-dep path triggers eviction of `tx_12` (and its descendants `tx_13`…`tx_24`).
4. `check_and_record_ancestors` calls `remove_entry_and_descendants(&tx_12_id)`, which removes 13 entries and decrements `self.total_tx_size` by their aggregate size `S_evicted`.
5. `add_entry` then writes `self.total_tx_size = total_tx_size` (the pre-eviction snapshot + `tx_A.size`), restoring the inflated value.
6. `total_tx_size` is now inflated by `S_evicted`. Repeat steps 2–5 until `total_tx_size` exceeds `max_tx_pool_size`.
7. All subsequent `send_transaction` RPC calls return `Reject::Full` even though the pool contains far fewer bytes than the limit.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-219)
```rust
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
```

**File:** tx-pool/src/component/pool_map.rs (L235-249)
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
```

**File:** tx-pool/src/component/pool_map.rs (L582-588)
```rust
    /// Check ancestors and record for entry
    // FIXME: In the scenario that a transaction passed all RBF rules, and then removed the conflicted
    // transaction in txpool, then failed with max ancestor limits, we now need to rollback the removing.
    // this is not an issue currently, because RBF have a rule that not allow any unknown inputs except
    // the conflicted inputs, so the new transaction can not be in a long transaction chain.
    // but it's still safer to report an error before any writing kind of operation.
    fn check_and_record_ancestors(
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
