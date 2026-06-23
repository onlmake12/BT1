### Title
`PoolMap::add_entry` Overwrites `total_tx_size`/`total_tx_cycles` With Stale Pre-Computed Values After In-Flight Evictions — (`tx-pool/src/component/pool_map.rs`)

### Summary

In `PoolMap::add_entry`, the new aggregate totals (`total_tx_size`, `total_tx_cycles`) are computed **before** `check_and_record_ancestors` is called. `check_and_record_ancestors` can evict existing pool entries (calling `remove_entry` → `update_stat_for_remove_tx`, which decrements `self.total_tx_size`). The final assignment then **overwrites** the correctly-updated field with the stale pre-computed value, causing a permanent overcount. This is the direct CKB analog of the Union Finance M-07 pattern: an invariant-critical aggregate is updated without re-validating the invariant after the inner mutation.

### Finding Description

In `tx-pool/src/component/pool_map.rs`, `add_entry` executes as follows:

```
line 210: let (total_tx_size, total_tx_cycles) =
              self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
              // Snapshot: new_total = self.total_tx_size + entry.size

line 213: evicts = self.check_and_record_ancestors(&mut entry)?;
              // May call remove_entry() → update_stat_for_remove_tx()
              // which DECREMENTS self.total_tx_size by evicted sizes

line 218: self.total_tx_size = total_tx_size;   // ← OVERWRITES the decremented value
line 219: self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

`updated_stat_for_add_tx` only checks for arithmetic overflow and returns the candidate totals; it does not commit them: [2](#0-1) 

`remove_entry` (called by any eviction path inside `check_and_record_ancestors`) calls `update_stat_for_remove_tx`, which decrements `self.total_tx_size` in place: [3](#0-2) [4](#0-3) 

After `check_and_record_ancestors` returns, `self.total_tx_size` reflects `original − evicted_sizes`. The assignment at line 218 then sets it back to `original + entry.size`, silently discarding the eviction credits. The net result is:

```
actual pool size  = original − evicted_sizes + entry.size
reported total    = original + entry.size          (overcounted by evicted_sizes)
```

The eviction path is confirmed by the caller in `process.rs`: the returned `evicts` are passed to `call_reject` (a notification callback), not to any pool-removal function, proving the entries were already removed inside `add_entry`: [5](#0-4) 

### Impact Explanation

`total_tx_size` is the sole gate for the pool-size eviction loop in `limit_size`: [6](#0-5) 

An overcounted `total_tx_size` causes `limit_size` to evict **additional legitimate transactions** that would not have been evicted had the accounting been correct. Concretely:

- Transactions with valid fee rates are silently dropped from the pool and rejected with `Reject::Full`, even though the pool has physical space.
- The `tx_pool_info` RPC reports an inflated `total_tx_size`, misleading operators and tooling.
- In `_update_tx_pool_for_reorg`, `limit_size` is called with `current_entry_id = None`, meaning the overcount can cause arbitrary pending/proposed transactions to be evicted during every reorg. [7](#0-6) 

### Likelihood Explanation

The trigger condition is submitting a transaction whose inputs consume a cell that is simultaneously used as a `cell_dep` by one or more existing pool transactions. This is a standard, unprivileged RPC call (`send_transaction`). Any external transaction sender can craft such a transaction. The eviction path inside `check_and_record_ancestors` is exercised whenever this cell-dep conflict arises, which is a documented and tested scenario (the `TxPoolLimitAncestorCount` integration test exercises related eviction paths). [8](#0-7) 

### Recommendation

Move the stat update **after** all mutations are complete, computing the final totals from the live fields rather than from a pre-mutation snapshot:

```rust
// Remove the pre-computation of (total_tx_size, total_tx_cycles) before check_and_record_ancestors.
// After insert_entry and all evictions are done, recompute:
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Now update totals using the live self.total_tx_size (already decremented by evictions):
self.total_tx_size = self.total_tx_size
    .checked_add(entry.size)
    .ok_or_else(|| Reject::Full(...))?;
self.total_tx_cycles = self.total_tx_cycles
    .checked_add(entry.cycles)
    .ok_or_else(|| Reject::Full(...))?;
```

Alternatively, add a post-insertion invariant assertion (`debug_assert_eq!(self.total_tx_size, self.recompute_total_stat().unwrap().0)`) to catch drift in testing.

### Proof of Concept

1. Fill the pool with transactions `T1…Tn` where each `Ti` declares a `cell_dep` on output `O` of some on-chain transaction `P`.
2. Submit a new transaction `T_attack` whose **input** spends output `O` of `P`. This forces `check_and_record_ancestors` to evict `T1…Tn` (they can no longer reference `O` as a live cell dep).
3. Inside `add_entry`: `total_tx_size` is pre-computed as `(sum of T1…Tn sizes) + T_attack.size`. `check_and_record_ancestors` removes `T1…Tn`, decrementing `self.total_tx_size` to `0`. The final assignment sets `self.total_tx_size = (sum of T1…Tn sizes) + T_attack.size` — an overcount of `sum(T1…Tn sizes)`.
4. `limit_size` is then called. Because `total_tx_size` is massively overcounted, it evicts additional transactions from the pool (including `T_attack` itself or other unrelated pending transactions), even though the pool is physically nearly empty.
5. Observe via `tx_pool_info` RPC that `total_tx_size` is inflated and that valid transactions are rejected with `Reject::Full`. [9](#0-8) [10](#0-9)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L192-221)
```rust
    /// Inesrt a `TxEntry` into pool_map.
    ///
    /// ## Returns
    ///
    /// Returns `Reject` when any error happened, otherwise return `Ok((succ, evicts))`
    /// - succ  : means whether the entry is inserted actually into pool,
    /// - evicts: is the evicted transactions before inserting this `TxEntry`,
    ///   Currently, evicts when inserting is only due to referring cell dep will be consumed by this new transaction.
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

**File:** tx-pool/src/component/pool_map.rs (L731-758)
```rust
    /// Update size and cycles statistics for remove tx
    /// cycles overflow is possible, currently obtaining cycles is not accurate
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

**File:** tx-pool/src/process.rs (L136-147)
```rust
                let may_recovered_txs = self.process_rbf(tx_pool, &entry, &conflicts);
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
```

**File:** tx-pool/src/process.rs (L1109-1114)
```rust
    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
}
```

**File:** tx-pool/src/pool.rs (L292-329)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
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
    }
```
