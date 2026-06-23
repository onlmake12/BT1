### Title
`total_tx_size` Inflation via Stale Local Variable Overwrite After Cell-Ref-Parent Eviction — (`tx-pool/src/component/pool_map.rs`)

### Summary

In `add_entry`, `updated_stat_for_add_tx` computes the new totals into **local variables** before `check_and_record_ancestors` runs. When that function evicts `cell_ref_parents`, each eviction calls `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size`. However, at the end of `add_entry`, the stale local variable — computed before any eviction — unconditionally overwrites `self.total_tx_size`, erasing all the decrements. The result is a permanently inflated `total_tx_size` that causes `limit_size` to prematurely evict legitimate transactions.

---

### Finding Description

The exact sequence in `add_entry`: [1](#0-0) 

```
Step 1 (line 210-211): local_total = self.total_tx_size + new_tx.size
Step 2 (line 213):     check_and_record_ancestors may call remove_entry_and_descendants
                         → remove_entry → update_stat_for_remove_tx
                         → self.total_tx_size -= evicted_tx.size   (correct decrement)
Step 3 (line 218):     self.total_tx_size = local_total            (OVERWRITES the decrement)
```

`updated_stat_for_add_tx` only reads `self.total_tx_size` and returns a new value; it does not write to `self`: [2](#0-1) 

`update_stat_for_remove_tx`, called inside every `remove_entry`, **does** write to `self.total_tx_size`: [3](#0-2) 

The eviction path inside `check_and_record_ancestors` is reached when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`: [4](#0-3) 

After the loop, `self.total_tx_size` correctly equals `old_total − Σ(evicted sizes)`. Line 218 then overwrites it with `old_total + new_tx.size`, inflating it by exactly `Σ(evicted sizes)`.

The inflation is **permanent**: subsequent calls to `updated_stat_for_add_tx` read the already-inflated `self.total_tx_size` and propagate the error forward. The only recovery path (`recompute_total_stat`) is only triggered on underflow, not on inflation: [5](#0-4) 

---

### Impact Explanation

`limit_size` compares `self.pool_map.total_tx_size` against `self.config.max_tx_pool_size` and evicts the lowest-fee-rate pending transactions until the pool is within bounds: [6](#0-5) 

`limit_size` is called immediately after every successful `_submit_entry`: [7](#0-6) 

With an inflated `total_tx_size`, `limit_size` evicts legitimate transactions that would not have been evicted under the true pool size. Each trigger of the bug inflates the counter by the sum of evicted tx sizes. Because the inflation accumulates across submissions, an attacker can repeatedly trigger the condition to drive `total_tx_size` arbitrarily above the real value, causing continuous premature eviction of honest transactions and degrading pool throughput.

---

### Likelihood Explanation

The trigger condition requires a new transaction whose ancestor count exceeds `max_ancestors_count` (default 25) but where the excess ancestors are all `cell_ref_parents` (transactions that are referenced as cell deps by an input of the new tx). An attacker can construct this deliberately:

1. Submit a chain of 25+ transactions where some intermediate transactions are also used as cell deps by later transactions.
2. Submit a final transaction that references those cell-dep ancestors, pushing `ancestors_count` just above the limit while keeping `cell_ref_parents` large enough to satisfy the eviction branch condition.

This requires only valid transaction fees and no privileged access. The path is reachable via standard P2P transaction relay (`submit_remote_tx`) or RPC (`send_transaction`).

---

### Recommendation

Replace the local-variable pattern with a direct update after eviction. The simplest correct fix is to move the stat update to **after** `check_and_record_ancestors` completes, so it sees the post-eviction `self.total_tx_size`:

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

Alternatively, call `recompute_total_stat` after any eviction occurs, or subtract evicted sizes from the pre-computed local variable before assigning it.

---

### Proof of Concept

```
Setup:
  max_ancestors_count = 25
  max_tx_pool_size    = 1_000_000 bytes
  current pool:       24 txs in a chain (tx0 → tx1 → … → tx23), each 1000 bytes
                      tx0 is also used as a cell dep by tx_new
  total_tx_size before = 24_000

Submit tx_new (inputs tx23's output, cell_dep = tx0's output):
  ancestors = {tx0..tx23} → ancestors_count = 25 (= max_ancestors_count, no eviction)

Now add tx_extra that has tx0..tx24 as ancestors (26 total) and tx0 as cell_ref_parent:
  ancestors_count = 26 > 25
  cell_ref_parents = {tx0}, so 26 - 1 = 25 ≤ 25 → eviction branch taken

  updated_stat_for_add_tx: local_total = 24_000 + tx_extra.size (e.g. 1000) = 25_000
  check_and_record_ancestors evicts tx0 (and its descendants if any):
    update_stat_for_remove_tx: self.total_tx_size = 24_000 - 1000 = 23_000
  insert tx_extra: self.total_tx_size = local_total = 25_000  ← BUG

  True value should be: 23_000 + 1000 = 24_000
  Actual self.total_tx_size = 25_000  (inflated by 1000 = evicted tx0 size)

Repeat N times → total_tx_size inflated by N × evicted_size
→ limit_size fires and evicts honest transactions even though real pool size is within bounds
```

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

**File:** tx-pool/src/process.rs (L137-152)
```rust
                let evicted = _submit_entry(tx_pool, status, entry.clone(), &self.callbacks)?;

                // in a corner case, a tx with lower fee rate may be rejected immediately
                // after inserting into pool, return proper reject error here
                for evict in evicted {
                    let reject = Reject::Invalidated(format!(
                        "invalidated by tx {}",
                        evict.transaction().hash()
                    ));
                    self.callbacks.call_reject(tx_pool, &evict, reject);
                }

                tx_pool.remove_conflict(&entry.proposal_short_id());
                tx_pool
                    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
                    .map_or(Ok(()), Err)?;
```
