Audit Report

## Title
Stale `total_tx_size`/`total_tx_cycles` Snapshot Overwritten After Ancestor-Eviction Inflates Pool Counters — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

`PoolMap::add_entry` snapshots `total_tx_size` and `total_tx_cycles` via `updated_stat_for_add_tx` before calling `check_and_record_ancestors`, which may evict pool entries and correctly decrement those fields in-place via `update_stat_for_remove_tx`. The stale pre-eviction snapshot is then unconditionally written back at lines 218–219, permanently inflating both counters by the aggregate size/cycles of every entry evicted during ancestor-limit enforcement. The subsequent `limit_size` call drives its eviction loop off the inflated `total_tx_size`, causing additional legitimate transactions to be expelled from the pool.

## Finding Description

In `add_entry` (lines 200–221):

```
Line 210-211: snapshot = self.total_tx_size + entry.size   // &self, no mutation
Line 213:     check_and_record_ancestors(...)               // may call remove_entry_and_descendants
                → update_stat_for_remove_tx(...)            // decrements self.total_tx_size in-place
Lines 218-219: self.total_tx_size = snapshot               // OVERWRITES the decrement
```

`updated_stat_for_add_tx` takes `&self` and returns `self.total_tx_size.checked_add(tx_size)` without touching the field. [1](#0-0) 

`check_and_record_ancestors` enters the eviction branch when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`, calling `remove_entry_and_descendants` for each evicted candidate. [2](#0-1) 

`update_stat_for_remove_tx` correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in-place. [3](#0-2) 

The write-back at lines 218–219 then overwrites those decrements with the stale snapshot, leaving `total_tx_size` inflated by exactly `Σ size(evicted_i)`. [4](#0-3) 

`limit_size` drives its eviction loop with `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, so phantom inflation causes it to expel additional legitimate transactions. [5](#0-4) 

## Impact Explanation

Each triggering of the eviction branch permanently inflates `total_tx_size` and `total_tx_cycles`. Because `limit_size` is called after every submission and every reorg, the inflation causes legitimate pending/proposed transactions to be silently dropped from the mempool without any protocol-level error. The pool's RPC-reported `total_tx_size` becomes incorrect, misleading operators and fee-estimation logic. This is a concrete, repeatable accounting corruption in the CKB tx-pool — a suboptimal (incorrect) implementation of the node's pool-size management mechanism. This maps to **Medium (2001–10000 points): Suboptimal implementation of CKB state storage/pool mechanism**.

## Likelihood Explanation

The trigger requires: (a) a transaction chain of length `max_ancestors_count - 1` sharing a cell dep with existing pool entries, and (b) a new transaction referencing that dep. Both conditions are constructible by any unprivileged tx submitter with no key material or hash power. The attack is repeatable — each iteration compounds the inflation — and requires no victim mistakes or external context.

## Recommendation

Remove the pre-computed snapshot and instead apply the new entry's contribution to the already-correct post-eviction `self.total_tx_size`:

```rust
// Validate overflow only (don't snapshot)
self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Evictions happen here, correctly decrementing self.total_tx_size
evicts = self.check_and_record_ancestors(&mut entry)?;

self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Apply new entry's contribution to the post-eviction totals
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

## Proof of Concept

1. Fill the pool with chain `tx0 → tx1 → … → tx_{N-1}` where `N = max_ancestors_count - 1`; each `tx_i` uses `cell_dep_A`.
2. Submit `tx_N` spending an output of `tx_{N-1}` and also referencing `cell_dep_A`. `ancestors_count = N+1 > max_ancestors_count`, but `cell_ref_parents` brings it within limit, triggering the eviction branch.
3. `check_and_record_ancestors` evicts e.g. `tx_{N-1}` (lowest fee); `update_stat_for_remove_tx` decrements `self.total_tx_size` by `size(tx_{N-1})`.
4. `add_entry` writes back `total_tx_size = old_total + size(tx_N)`, ignoring the decrement. Actual value is `old_total + size(tx_N) + size(tx_{N-1})` instead of `old_total + size(tx_N) - size(tx_{N-1})`.
5. If the pool was near capacity, `limit_size` sees `total_tx_size > max_tx_pool_size` and evicts an additional legitimate transaction of size ≈ `size(tx_{N-1})`.
6. Repeat from step 2; each iteration compounds the inflation and expels more legitimate transactions.

A unit test can assert `pool_map.total_tx_size == pool_map.recompute_total_stat().unwrap().0` after each `add_entry` call that triggers the eviction branch, which will fail on the current code.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
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
