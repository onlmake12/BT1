### Title
Stale Pre-Eviction Pool-Size Overwrite Inflates `total_tx_size`/`total_tx_cycles` — (`File: tx-pool/src/component/pool_map.rs`)

### Summary
In `PoolMap::add_entry`, the new pool-wide totals (`total_tx_size`, `total_tx_cycles`) are computed **before** `check_and_record_ancestors` runs. That function can evict existing pool entries, each of which calls `update_stat_for_remove_tx` and correctly decrements the live fields. Immediately afterward, `add_entry` blindly overwrites those live fields with the stale pre-eviction snapshot, silently re-inflating the totals by the size and cycles of every evicted transaction. This is the direct CKB analog of the `safeApprove` pattern: a value is set to a fixed amount computed before a state-changing operation, ignoring the intermediate mutations.

### Finding Description

`PoolMap::add_entry` (`tx-pool/src/component/pool_map.rs`) executes in this order:

```
1. (total_tx_size, total_tx_cycles) = updated_stat_for_add_tx(entry.size, entry.cycles)
   // snapshot = self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles

2. evicts = check_and_record_ancestors(&mut entry)
   // may call remove_entry_and_descendants → update_stat_for_remove_tx
   // which DECREMENTS self.total_tx_size and self.total_tx_cycles for each evicted tx

3. self.total_tx_size  = total_tx_size   // OVERWRITES the decremented live value
4. self.total_tx_cycles = total_tx_cycles // OVERWRITES the decremented live value
``` [1](#0-0) 

After step 2, `self.total_tx_size` correctly reflects the pool minus the evicted entries. Steps 3–4 restore it to the pre-eviction snapshot plus the new entry, so the evicted entries' bytes and cycles are never subtracted. The code itself acknowledges the fragility of this accounting: [2](#0-1) 

The eviction path inside `check_and_record_ancestors` is triggered when a submitted transaction has more than `max_ancestors_count` ancestors but the excess is attributable to `cell_ref_parents` (cell-dep parents), allowing the pool to evict those lower-fee parents to make room: [3](#0-2) 

Each evicted entry's removal correctly calls `update_stat_for_remove_tx`: [4](#0-3) 

But the result is immediately discarded by the overwrite in `add_entry`.

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size` to decide whether to evict further transactions: [5](#0-4) 

An inflated `total_tx_size` causes `limit_size` to keep evicting legitimate pending transactions even though the pool is actually below the configured `max_tx_pool_size`. Each subsequent `add_entry` that triggers the ancestor-eviction path compounds the inflation. Over time the pool's reported size diverges from its real size, causing:

- Legitimate transactions to be evicted unnecessarily (service degradation for honest users).
- The pool to reject new submissions with `Reject::Full` even when real capacity exists.
- Fee-rate estimation (`estimate_fee_rate`) to operate on a pool that appears larger than it is, skewing the returned fee rate upward.

### Likelihood Explanation

The trigger requires an unprivileged tx-pool submitter to:
1. Submit a chain of transactions up to `max_ancestors_count − 1` deep (default 25).
2. Submit a transaction whose cell-deps reference one or more of those in-pool transactions, pushing the ancestor count over the limit.

Both steps use only the standard `send_transaction` RPC, which is open to any node peer or RPC caller. No privileged access, key material, or majority hash power is required. The default `max_ancestors_count` of 25 is easily reachable on mainnet.

### Recommendation

Move the computation of `total_tx_size`/`total_tx_cycles` to **after** `check_and_record_ancestors` completes, so any evictions are already reflected in `self.total_tx_size`/`self.total_tx_cycles` before the new entry's contribution is added:

```rust
// Remove the early snapshot:
// let (total_tx_size, total_tx_cycles) = self.updated_stat_for_add_tx(...)?;

evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Compute totals AFTER evictions have already updated self.total_tx_*:
let (total_tx_size, total_tx_cycles) = self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size  = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, call `recompute_total_stat()` unconditionally at the end of `add_entry` whenever evictions occurred.

### Proof of Concept

Assume `max_tx_pool_size = 180 MB`, `max_ancestors_count = 25`.

1. Submit 24 chained transactions `T1 → T2 → … → T24` (each ~1 KB). Pool `total_tx_size ≈ 24 KB`.
2. Submit `T25` whose cell-dep references `T1` (making ancestor count = 25 + 1 = 26 > 25, with `cell_ref_parents = {T1}`).
3. `check_and_record_ancestors` evicts `T1` (size ~1 KB): `update_stat_for_remove_tx` sets `self.total_tx_size = 23 KB`.
4. `add_entry` then executes `self.total_tx_size = total_tx_size` where `total_tx_size` was snapshotted as `24 KB + 1 KB = 25 KB`.
5. Pool now reports `total_tx_size = 25 KB` but actually holds only `T2…T24 + T25 = 24 KB`.
6. Repeating this pattern N times inflates `total_tx_size` by ~1 KB per iteration, eventually causing `limit_size` to evict honest transactions even though the pool is not actually full. [6](#0-5) [7](#0-6)

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

**File:** tx-pool/src/component/pool_map.rs (L246-248)
```rust
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
            entry.inner
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

**File:** tx-pool/src/component/pool_map.rs (L731-733)
```rust
    /// Update size and cycles statistics for remove tx
    /// cycles overflow is possible, currently obtaining cycles is not accurate
    fn update_stat_for_remove_tx(&mut self, tx_size: usize, cycles: Cycle) {
```

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```
