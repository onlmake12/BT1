### Title
Stale `total_tx_size`/`total_tx_cycles` Overwrite After Ancestor Eviction Inflates Pool Size Accounting — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new pool-size totals are computed **before** `check_and_record_ancestors` runs. That inner call can evict transactions (and correctly decrements `self.total_tx_size`/`self.total_tx_cycles` via `update_stat_for_remove_tx`), but the stale pre-eviction totals are then unconditionally written back, erasing the decrements. The pool permanently believes it holds more data than it actually does.

---

### Finding Description

`add_entry` in `pool_map.rs` follows this sequence:

```rust
// Step 1 – compute new totals BEFORE any eviction
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // = S + new_size

// Step 2 – may evict transactions; each eviction calls
//           update_stat_for_remove_tx → self.total_tx_size -= evicted_size
//           so self.total_tx_size is now S - evicted_size
evicts = self.check_and_record_ancestors(&mut entry)?;

// Step 3 – overwrite with the stale pre-eviction value
self.total_tx_size  = total_tx_size;   // written as S + new_size  ← BUG
self.total_tx_cycles = total_tx_cycles; // same problem
``` [1](#0-0) 

`updated_stat_for_add_tx` simply adds the new entry's size to the current totals and returns the result as a local variable: [2](#0-1) 

`check_and_record_ancestors` calls `remove_entry_and_descendants` when the ancestor count exceeds `max_ancestors_count` and cell-ref parents can be evicted: [3](#0-2) 

`remove_entry` (called for every evicted tx) correctly decrements `self.total_tx_size`: [4](#0-3) 

After all evictions, `self.total_tx_size = S − evicted_size`. Step 3 then overwrites it with `S + new_size`, so the final value is inflated by exactly `evicted_size` instead of the correct `S + new_size − evicted_size`.

---

### Impact Explanation

`total_tx_size` is the sole counter used by `limit_size` to decide whether to evict further transactions: [5](#0-4) 

Because the counter is permanently inflated after each eviction event, two concrete effects follow:

1. **Spurious further evictions** — `limit_size` is called after `add_entry` returns. It sees an inflated `total_tx_size > max_tx_pool_size` and evicts additional valid, fee-paying transactions that should have remained in the pool.
2. **Premature rejection of future submissions** — subsequent calls to `updated_stat_for_add_tx` start from an already-inflated baseline, causing valid transactions to be rejected with `Reject::Full` even when the pool has real capacity.

Both effects degrade mempool throughput and can be exploited to selectively starve the pool of legitimate transactions.

---

### Likelihood Explanation

The eviction path inside `check_and_record_ancestors` is triggered whenever:

- A new transaction's cell dep is already consumed by an in-pool transaction (`cell_ref_parents` is non-empty), **and**
- The total ancestor count (including those cell-ref parents) exceeds `max_ancestors_count` (default 25).

Any unprivileged RPC caller (`send_transaction`) or relay peer can craft such a transaction. The condition is not exotic; it arises naturally in chains of transactions that share cell deps, and can be deliberately engineered by an attacker who observes the mempool state via `get_raw_tx_pool`.

---

### Recommendation

Compute the new totals **after** `check_and_record_ancestors` completes, so that any eviction-driven decrements are already reflected in `self.total_tx_size`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Re-read the (now post-eviction) totals and add only the new entry's contribution
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size  = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, drop the local variables entirely and apply the increment in-place after all mutations are complete.

---

### Proof of Concept

1. Fill the pool with a chain of 24 transactions `T1 → T2 → … → T24` where each spends the previous output.
2. Submit a separate transaction `R` that takes `T1`'s output as a **cell dep** (making `R` a `cell_ref_parent` of the chain).
3. Submit a new transaction `T_new` that **spends** `T1`'s output (consuming the same cell dep). `T_new` now has 24 ancestors + `R` as a cell-ref parent, exceeding `max_ancestors_count = 25`.
4. `check_and_record_ancestors` evicts `R` (size `S_R`) via `remove_entry_and_descendants`, correctly decrementing `self.total_tx_size` by `S_R`.
5. Step 3 of `add_entry` then overwrites `self.total_tx_size` with the pre-eviction value, inflating it by `S_R`.
6. Observe via `get_tip_tx_pool_info` that `total_tx_size` is larger than the sum of actual entry sizes, and that `limit_size` subsequently evicts a valid transaction that fits within `max_tx_pool_size`.

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

**File:** tx-pool/src/component/pool_map.rs (L615-625)
```rust
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
