Audit Report

## Title
Stale Pre-Eviction Snapshot Overwrites Live `total_tx_size`/`total_tx_cycles` Counters in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, new pool-size totals are captured into local variables before `check_and_record_ancestors` runs. When that function evicts transactions, `update_stat_for_remove_tx` correctly decrements `self.total_tx_size`/`self.total_tx_cycles` in place. The stale pre-eviction snapshot is then unconditionally written back at lines 218–219, silently cancelling every decrement and permanently inflating the counters by the aggregate size/cycles of all evicted entries. Repeated exploitation drives the counters arbitrarily high, causing `limit_size` to continuously evict legitimate transactions and reject new submissions with `Reject::Full`.

## Finding Description
`add_entry` (lines 200–221) executes this sequence:

1. **Lines 210–211**: `updated_stat_for_add_tx` takes `&self`, computes `self.total_tx_size + entry.size` and `self.total_tx_cycles + entry.cycles`, and returns them as local variables. It does not write to `self`.

2. **Line 213**: `check_and_record_ancestors` takes `&mut self`. When `ancestors_count > max_ancestors_count` (line 598) but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (line 603), the eviction branch (lines 615–625) calls `remove_entry_and_descendants` for each evicted entry. This reaches `update_stat_for_remove_tx` (lines 733–758), which **directly writes** decremented values to `self.total_tx_size` and `self.total_tx_cycles`.

3. **Lines 218–219**: `self.total_tx_size = total_tx_size` and `self.total_tx_cycles = total_tx_cycles` unconditionally overwrite the correctly-decremented live values with the stale pre-eviction snapshot.

Concrete arithmetic (pool at 100 bytes, new tx = 10 bytes, evicted tx = 20 bytes):
- After `updated_stat_for_add_tx`: local snapshot = 110, `self.total_tx_size` = 100 (unchanged)
- After eviction via `update_stat_for_remove_tx`: `self.total_tx_size` = 80 (correct)
- After line 218 assignment: `self.total_tx_size` = 110 (wrong; should be 90)

The eviction branch is reachable whenever a new transaction's ancestor set contains cell-ref parents that push `ancestors_count` just over `max_ancestors_count`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

## Impact Explanation
`total_tx_size` is the sole guard in `limit_size` (pool.rs line 298):
```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size { ... }
```
An inflated counter causes `limit_size` to believe the pool is over capacity, triggering cascading spurious evictions of legitimate fee-paying transactions. Repeated exploitation drives the counter arbitrarily high without filling the pool with real data, causing the node to continuously reject new submissions with `Reject::Full` and evict its own contents. Since all nodes run identical code and process the same relayed transactions, this can be applied network-wide with low cost. This matches the **High** impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* [5](#0-4) 

## Likelihood Explanation
The trigger condition is fully under attacker control with no privileged access required. An unprivileged sender can craft transactions where a base transaction's output is used as a cell dep by multiple pool transactions, then submit a new transaction spending that base output, pushing `ancestors_count` just over `max_ancestors_count` while keeping `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. This is reachable via the standard `send_transaction` RPC or P2P relay path. The attack is repeatable with fresh transactions to accumulate unbounded inflation.

## Recommendation
Compute the new totals **after** all evictions have completed. Remove the pre-eviction snapshot and instead apply the addition directly after `check_and_record_ancestors` returns:

```rust
// Remove: let (total_tx_size, total_tx_cycles) = self.updated_stat_for_add_tx(...)?;
// Keep the overflow check as a read-only validation:
self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Apply addition AFTER evictions have already decremented the counters:
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

## Proof of Concept
**Setup**: `max_ancestors_count = 25`, `max_tx_pool_size = 1_000_000` bytes.
1. Submit base tx `T0`. Submit 24 transactions `T1…T24`, each referencing an output of `T0` as a cell dep (~1,000 bytes each). Pool: 25 entries, `total_tx_size ≈ 25,000`.
2. Submit `Tnew` (1,000 bytes) spending an output of `T0`. Ancestor set includes `T1…T24` via cell-dep linkage → `ancestors_count = 26 > 25`. `cell_ref_parents = {T1…T24}`, so `26 - 24 = 2 ≤ 25` — eviction branch taken.
3. `check_and_record_ancestors` evicts e.g. `T1`, `T2` (each 1,000 bytes). `update_stat_for_remove_tx` correctly sets `self.total_tx_size = 23,000`.
4. Line 218 writes back stale snapshot: `self.total_tx_size = 26,000` (should be `24,000`). Inflation: +2,000 bytes per call.
5. Repeat ~490 times. `total_tx_size` reaches `≈ 1,000,000` while actual pool is nearly empty. `limit_size` fires on every subsequent `add_entry`, evicting legitimate transactions and returning `Reject::Full` to all new submissions.

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
