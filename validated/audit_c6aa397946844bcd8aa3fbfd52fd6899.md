Audit Report

## Title
`total_tx_size` Permanently Inflated by Stale Local Variable Overwrite After Cell-Ref-Parent Eviction — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `add_entry`, `updated_stat_for_add_tx` computes new pool totals into local variables before `check_and_record_ancestors` runs. When that function evicts `cell_ref_parents`, each eviction calls `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size`. However, lines 218–219 then unconditionally overwrite `self.total_tx_size` with the stale pre-eviction local variable, erasing all decrements. The result is a permanently inflated `total_tx_size` that causes `limit_size` to prematurely evict legitimate pending transactions.

## Finding Description
In `add_entry` ( [1](#0-0) ), `updated_stat_for_add_tx` is called at lines 210–211 and captures `self.total_tx_size + entry.size` into a local variable before any eviction occurs. `check_and_record_ancestors` is then called at line 213; when `ancestors_count > max_ancestors_count` but `ancestors_count.saturating_sub(cell_ref_parents.len()) <= max_ancestors_count` ( [2](#0-1) ), it calls `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size` ( [3](#0-2) ). After all evictions, lines 218–219 unconditionally assign the stale local `total_tx_size` back to `self.total_tx_size`, erasing every decrement performed during eviction. `updated_stat_for_add_tx` is a `&self` read-only method ( [4](#0-3) ) and `update_stat_for_remove_tx` is `&mut self` writing directly to `self.total_tx_size` ( [5](#0-4) ). The only recovery path, `recompute_total_stat` ( [6](#0-5) ), is only triggered on underflow, never on inflation, so the overcount is permanent and cumulative.

## Impact Explanation
`limit_size` in `pool.rs` loops while `self.pool_map.total_tx_size > self.config.max_tx_pool_size` and evicts the lowest-fee-rate pending transactions ( [7](#0-6) ). It is called immediately after every successful `_submit_entry` ( [8](#0-7) ). With an inflated `total_tx_size`, `limit_size` evicts legitimate transactions that would not have been evicted under the true pool size. Because the inflation accumulates across submissions, an attacker can drive `total_tx_size` arbitrarily above the real value, causing continuous premature eviction of honest transactions. This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
The trigger requires a new transaction whose ancestor count exceeds `max_ancestors_count` (default 25) but where the excess ancestors are all `cell_ref_parents`. An attacker constructs this by submitting a chain of 25+ transactions where some intermediate transactions are also used as cell deps by later transactions, then submitting a final transaction that references those cell-dep ancestors. This pushes `ancestors_count` above the limit while keeping `cell_ref_parents` large enough to satisfy the eviction branch condition at line 603. The path is reachable via standard P2P transaction relay or RPC `send_transaction` with no privileged access. The condition is repeatable: each trigger inflates the counter by the sum of evicted tx sizes, and the inflation accumulates indefinitely.

## Recommendation
Move the stat update to after `check_and_record_ancestors` completes, so it reads the post-eviction `self.total_tx_size`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute totals AFTER eviction, not before
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

This ensures `updated_stat_for_add_tx` reads the already-decremented `self.total_tx_size` and the final assignment is correct.

## Proof of Concept
```
Setup:
  max_ancestors_count = 25
  max_tx_pool_size    = 1_000_000 bytes
  Pool contains tx0..tx23 (24 txs, each 1000 bytes), total_tx_size = 24_000
  tx0 is also used as a cell dep by tx_extra

Submit tx_trigger with tx0..tx24 as ancestors (26 total), tx0 as cell_ref_parent:
  ancestors_count = 26 > 25
  cell_ref_parents = {tx0}: 26 - 1 = 25 <= 25 → eviction branch taken

  Line 210-211: local_total = 24_000 + 1000 = 25_000
  Line 213: check_and_record_ancestors evicts tx0:
    update_stat_for_remove_tx: self.total_tx_size = 24_000 - 1000 = 23_000  ✓
  Line 218: self.total_tx_size = local_total = 25_000  ← BUG

  True value: 23_000 + 1000 = 24_000
  Actual self.total_tx_size = 25_000 (inflated by 1000)

Repeat N times → total_tx_size inflated by N × evicted_size
→ limit_size fires and evicts honest transactions even though real pool size is within bounds
```

A unit test against `PoolMap::add_entry` that sets up the above chain, calls `add_entry` for `tx_trigger`, and asserts `pool_map.total_tx_size == expected_true_size` will fail on the current code, confirming the bug.

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

**File:** tx-pool/src/process.rs (L150-152)
```rust
                tx_pool
                    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
                    .map_or(Ok(()), Err)?;
```
