The code confirms the claim exactly. Let me verify the `limit_size` usage and any related tests.

Audit Report

## Title
Global `total_tx_size`/`total_tx_cycles` Counters Inflated When Eviction Occurs During `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, the updated counter values are computed into local variables before `check_and_record_ancestors` runs. If that function evicts transactions via `remove_entry_and_descendants`, the correct decrements applied to `self.total_tx_size`/`self.total_tx_cycles` are immediately overwritten by the stale pre-eviction local variables at lines 218–219. Each eviction event permanently inflates the counters by the evicted transaction's size and cycles, and the inflation compounds with repeated triggering.

## Finding Description
In `add_entry` (lines 200–221):

1. **Lines 210–211**: `updated_stat_for_add_tx` computes `total_tx_size = self.total_tx_size + entry.size` and `total_tx_cycles = self.total_tx_cycles + entry.cycles` into **local variables**, snapshotting the pre-eviction state. [1](#0-0) 

2. **Line 213**: `check_and_record_ancestors` is called. When `ancestors_count > max_ancestors_count` and `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (lines 603–625), it calls `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size` and `self.total_tx_cycles`. [2](#0-1) [3](#0-2) [4](#0-3) 

3. **Lines 218–219**: The stale local variables (computed before eviction) are unconditionally written back to `self`, silently overwriting the correct decrements. [5](#0-4) 

Concrete arithmetic: if `self.total_tx_size = X` before `add_entry`, the local snapshot is `X + entry.size`. After eviction of a tx with `size = S`, `update_stat_for_remove_tx` sets `self.total_tx_size = X - S`. Lines 218–219 then overwrite with `X + entry.size`, yielding an inflation of `S` per eviction event. The `update_stat_for_remove_tx` underflow fallback at lines 742–756 only triggers on `checked_sub` failure, not on this inflation path. [6](#0-5) 

## Impact Explanation
The inflated counters directly affect two downstream consumers:
- **`limit_size`** (in `tx-pool/src/pool.rs`) uses `pool_map.total_tx_size` to decide whether to evict transactions. An inflated counter causes valid, fee-paying transactions to be evicted from the mempool unnecessarily, degrading mempool quality and reducing block packing efficiency.
- **`tx_pool_info` RPC** returns the inflated values, misleading node operators and tooling about actual pool utilization.

This constitutes a **suboptimal implementation of CKB state storage/accounting mechanism** (Medium, 2001–10000 points). [7](#0-6) 

## Likelihood Explanation
The eviction path requires: (1) a new transaction whose ancestor count exceeds `max_ancestors_count`, and (2) some ancestors are "cell-ref parents" sharing the same cell dep. An unprivileged submitter can craft a chain of transactions all referencing the same popular cell dep (e.g., a widely-used lock script), then submit a new transaction into that chain to trigger eviction. Each such submission inflates the counters by the evicted transaction's size/cycles. The setup requires confirmed UTXOs (non-zero cost), but the inflation compounds with each repetition. [8](#0-7) 

## Recommendation
Move the counter update to **after** `check_and_record_ancestors` completes, so evictions are already reflected in `self.total_tx_size`/`self.total_tx_cycles` before the new entry's contribution is added:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute and apply new entry's contribution after evictions are settled
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

This ensures the overflow pre-check and the final write both operate on the post-eviction state. [9](#0-8) 

## Proof of Concept
1. Fill the pool with a chain of N transactions (N = `max_ancestors_count`) all referencing the same cell dep `D`. Each tx has `size = S`, `cycles = C`. Record `total_tx_size` via `tx_pool_info` RPC.
2. Submit transaction `T_new` referencing cell dep `D` and spending an output of the chain. Its ancestor count = N+1 > `max_ancestors_count`, but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`, so the eviction branch at line 603 is taken.
3. `check_and_record_ancestors` evicts one chain tx (size=S, cycles=C). `self.total_tx_size` is decremented by S. Then lines 218–219 overwrite with `old_total + S_new`, ignoring the decrement.
4. Query `tx_pool_info`: `total_tx_size` is inflated by S compared to the actual sum of all entries in the pool.
5. Repeat step 2 with new transactions to compound the inflation and observe `limit_size` evicting valid transactions prematurely.

A unit test can assert `pool_map.total_tx_size == pool_map.entries.iter().map(|(_, e)| e.inner.size).sum::<usize>()` after triggering the eviction path, which will fail with the current code. [10](#0-9)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L68-71)
```rust
    // sum of all tx_pool tx's virtual sizes.
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

**File:** tx-pool/src/component/pool_map.rs (L247-247)
```rust
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
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

**File:** tx-pool/src/component/pool_map.rs (L738-740)
```rust
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L742-756)
```rust
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
```
