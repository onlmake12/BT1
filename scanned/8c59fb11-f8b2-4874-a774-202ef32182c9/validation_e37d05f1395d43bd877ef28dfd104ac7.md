I found the bug. Let me verify the exact sequence in `add_entry` and `check_and_record_ancestors` to confirm the accounting overwrite.### Title
Stale Pre-Eviction Snapshot Overwrites Correctly Updated `total_tx_size`/`total_tx_cycles` After Ancestor-Count Eviction — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new pool totals (`total_tx_size`, `total_tx_cycles`) are snapshotted **before** any eviction occurs. When `check_and_record_ancestors` subsequently evicts transactions to satisfy the ancestor-count limit, those evictions correctly decrement `self.total_tx_size` and `self.total_tx_cycles` via `update_stat_for_remove_tx`. However, the function then unconditionally overwrites those correctly-updated fields with the stale pre-eviction snapshot, erasing all eviction accounting. The pool permanently over-reports its occupied size and cycles by the total weight of every evicted transaction.

---

### Finding Description

`add_entry` in `tx-pool/src/component/pool_map.rs` follows this sequence:

```rust
// Step 1 – snapshot new totals BEFORE any eviction
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
//   = (self.total_tx_size + entry.size,
//      self.total_tx_cycles + entry.cycles)

// Step 2 – may evict N transactions; each calls
//   update_stat_for_remove_tx → self.total_tx_size -= evicted_size
evicts = self.check_and_record_ancestors(&mut entry)?;

// ... insert new entry ...

// Step 3 – OVERWRITES the correctly-updated fields with the stale snapshot
self.total_tx_size = total_tx_size;    // ignores all evictions
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

The eviction path inside `check_and_record_ancestors` is reached when a new transaction's ancestor count exceeds `max_ancestors_count` but can be reduced by evicting existing `cell_ref_parents`: [2](#0-1) 

Each evicted entry goes through `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size` and `self.total_tx_cycles`: [3](#0-2) 

But those decrements are immediately discarded when `add_entry` writes the stale snapshot back at lines 218–219. The correct post-operation value should be:

```
total_tx_size = (old_total - evicted_sizes) + new_entry_size
```

The actual value stored is:

```
total_tx_size = old_total + new_entry_size   // evicted_sizes never subtracted
```

`updated_stat_for_add_tx` itself is read-only and does not mutate `self`; it only computes a candidate value: [4](#0-3) 

---

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size` to decide whether to evict further transactions: [5](#0-4) 

Because `total_tx_size` is over-reported by the cumulative size of all ancestor-evicted transactions, `limit_size` will evict **additional** legitimate transactions that are not actually over the configured `max_tx_pool_size`. Each subsequent call to `add_entry` that triggers the ancestor-eviction path compounds the error. Over time:

1. **Legitimate transactions are spuriously evicted** from the pool by `limit_size`, causing valid pending transactions to be dropped and their submitters to receive `Reject::Full` errors.
2. **Future submissions are incorrectly rejected** by `updated_stat_for_add_tx` with `Reject::Full` even when real pool occupancy is well below the limit.
3. **RPC callers** reading `tx_pool_info` (`total_tx_size`, `total_tx_cycles`) receive permanently inflated values, breaking any tooling or fee-estimation logic that relies on those fields. [6](#0-5) 

---

### Likelihood Explanation

The eviction path is reachable by any unprivileged transaction sender via `send_raw_transaction` RPC or P2P transaction relay. The attacker needs only to submit a transaction whose inputs or cell-deps reference outputs of existing pool transactions such that the ancestor count exceeds `max_ancestors_count` (default 25) while `cell_ref_parents` is non-empty. This is a normal pattern for chained transactions that share a cell dep, and requires no special privilege, no key material, and no majority hash power. The condition is therefore reachable in ordinary mainnet operation, not only under adversarial conditions.

---

### Recommendation

Move the stat assignment **after** `check_and_record_ancestors` completes, and compute the final totals from the already-updated `self.total_tx_size`/`self.total_tx_cycles` rather than from a pre-eviction snapshot:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute totals AFTER evictions have already updated self.total_tx_size/cycles
self.total_tx_size = self.total_tx_size
    .checked_add(entry.size)
    .ok_or_else(|| Reject::Full(...))?;
self.total_tx_cycles = self.total_tx_cycles
    .checked_add(entry.cycles)
    .ok_or_else(|| Reject::Full(...))?;
```

The overflow check that was previously done in `updated_stat_for_add_tx` must be preserved; it can be re-applied at this later point using the post-eviction `self.total_tx_size`.

---

### Proof of Concept

**Setup:** Pool with `max_ancestors_count = 25` and `max_tx_pool_size = 10_000` bytes. Pool currently holds 24 transactions forming a chain (ancestors of a 25th), plus one additional transaction `C` that is a `cell_ref_parent` of the incoming transaction `T`.

**Steps:**

1. Submit transaction `T` via `send_raw_transaction`. `T` has 25 ancestors (exceeds limit) but `cell_ref_parents = {C}`, so `ancestors_count - cell_ref_parents.len() = 25 <= max_ancestors_count`.
2. `add_entry` is called:
   - `updated_stat_for_add_tx` snapshots `total_tx_size_snapshot = self.total_tx_size + size(T)`.
   - `check_and_record_ancestors` evicts `C` (and its descendants). `remove_entry` → `update_stat_for_remove_tx` correctly sets `self.total_tx_size -= size(C)`.
   - Lines 218–219 write `self.total_tx_size = total_tx_size_snapshot`, restoring the pre-eviction value. `size(C)` is never subtracted.
3. **Result:** `self.total_tx_size` is now `size(C)` bytes higher than the actual sum of entries in the pool.
4. Repeat with additional transactions that trigger the same path. Each iteration inflates `total_tx_size` further.
5. Eventually `total_tx_size > max_tx_pool_size` even though actual pool bytes are well below the limit. `limit_size` begins evicting valid transactions; new submissions receive `Reject::Full`. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L69-71)
```rust
    pub(crate) total_tx_size: usize,
    // sum of all tx_pool tx's cycles.
    pub(crate) total_tx_cycles: Cycle,
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

**File:** tx-pool/src/pool.rs (L298-326)
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
```
