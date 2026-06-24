Audit Report

## Title
`total_tx_size` Inflation via Stale Local Variable Overwrite After Cell-Ref-Parent Eviction — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `add_entry`, `updated_stat_for_add_tx` snapshots `self.total_tx_size + new_tx.size` into a local variable before `check_and_record_ancestors` runs. When that function evicts `cell_ref_parents`, each eviction calls `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size`. However, line 218 then unconditionally overwrites `self.total_tx_size` with the stale pre-eviction local, erasing all decrements and permanently inflating the tracked pool size by exactly `Σ(evicted sizes)`. This causes `limit_size` to prematurely evict legitimate transactions from the pool.

## Finding Description
The exact code path in `add_entry`: [1](#0-0) 

- **Lines 210–211**: `updated_stat_for_add_tx` is a read-only method that reads `self.total_tx_size`, adds `entry.size`, and returns the result as a local variable — it does not write to `self`. [2](#0-1) 

- **Line 213**: `check_and_record_ancestors` enters the eviction branch when `ancestors_count > max_ancestors_count` but `ancestors_count.saturating_sub(cell_ref_parents.len()) <= max_ancestors_count`. It calls `remove_entry_and_descendants` in a loop. [3](#0-2) 

- `remove_entry_and_descendants` calls `remove_entry` for each removed entry, which calls `update_stat_for_remove_tx`, correctly decrementing `self.total_tx_size`. [4](#0-3) 

- `update_stat_for_remove_tx` writes to `self.total_tx_size` via `checked_sub`. [5](#0-4) 

- **Line 218**: `self.total_tx_size = total_tx_size` overwrites the correctly-decremented `self.total_tx_size` with the stale pre-eviction snapshot. [6](#0-5) 

Net result: if evictions removed `Σ S_i` bytes, `self.total_tx_size` is inflated by exactly `Σ S_i`. The only recovery path, `recompute_total_stat`, is only triggered on underflow, never on inflation, so the error is permanent and accumulates across submissions. [7](#0-6) 

## Impact Explanation
`limit_size` loops while `self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting the lowest-fee-rate pending transactions. [8](#0-7) 

It is called immediately after every successful `_submit_entry`. [9](#0-8) 

With an inflated `total_tx_size`, `limit_size` evicts legitimate transactions that would not have been evicted under the true pool size. Because the inflation accumulates across submissions, an attacker can drive `total_tx_size` arbitrarily above the real value, causing continuous premature eviction of honest transactions and preventing their inclusion in blocks. This matches **High (10001–15000 points): "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation
The trigger requires a transaction whose ancestor count exceeds `max_ancestors_count` (default 25) but where the excess ancestors are all `cell_ref_parents` (used as cell deps). An unprivileged attacker can construct this deliberately with only valid transaction fees: submit a chain of 25+ transactions where some intermediate transactions are also used as cell deps, then submit a final transaction referencing those cell-dep ancestors. The path is reachable via standard P2P relay (`submit_remote_tx`) or RPC (`send_transaction`). The condition is repeatable, allowing inflation to accumulate indefinitely.

## Recommendation
Move the stat update to after `check_and_record_ancestors` completes, so it reads the post-eviction `self.total_tx_size`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute totals AFTER eviction
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, subtract the sizes of all evicted transactions from the pre-computed local variable before assigning, or call `recompute_total_stat` whenever evictions occur.

## Proof of Concept
```
Setup:
  max_ancestors_count = 25
  max_tx_pool_size    = 1_000_000 bytes
  Pool: tx0..tx23 in a chain (24 txs, each 1000 bytes)
  tx0 is also used as a cell dep by tx_new
  total_tx_size before = 24_000

Submit tx_extra (ancestors_count = 26 > 25,
                 cell_ref_parents = {tx0}, 26 - 1 = 25 ≤ 25 → eviction branch):

  updated_stat_for_add_tx: local_total = 24_000 + 1000 = 25_000
  check_and_record_ancestors evicts tx0:
    update_stat_for_remove_tx: self.total_tx_size = 24_000 - 1000 = 23_000
  Line 218: self.total_tx_size = 25_000  ← BUG (correct value: 23_000 + 1000 = 24_000)
  Inflation = 1000 bytes per trigger

Repeat N times → total_tx_size inflated by N × evicted_size
→ limit_size fires and evicts honest transactions even though real pool is within bounds
```

A unit test can confirm this by asserting `pool_map.total_tx_size` equals `recompute_total_stat()` after the trigger sequence.

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

**File:** tx-pool/src/component/pool_map.rs (L733-741)
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

**File:** tx-pool/src/process.rs (L150-152)
```rust
                tx_pool
                    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
                    .map_or(Ok(()), Err)?;
```
