### Title
Stale Pre-Eviction Snapshot Overwrites Live `total_tx_size`/`total_tx_cycles` Accounting in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new aggregate statistics (`total_tx_size`, `total_tx_cycles`) are snapshotted **before** `check_and_record_ancestors` runs. That inner call can evict conflicting pool entries via `remove_entry`, which correctly decrements `self.total_tx_size` and `self.total_tx_cycles` through `update_stat_for_remove_tx`. However, `add_entry` then unconditionally overwrites those live fields with the stale pre-eviction snapshot, erasing the decrements. The result is that `total_tx_size` and `total_tx_cycles` are permanently inflated by the aggregate size/cycles of every evicted entry, for every such insertion event.

---

### Finding Description

`PoolMap` maintains two aggregate accounting fields:

```
pub(crate) total_tx_size: usize,   // sum of all pool tx virtual sizes
pub(crate) total_tx_cycles: Cycle, // sum of all pool tx cycles
``` [1](#0-0) 

`add_entry` is the single insertion path for all three pool states (Pending, Gap, Proposed):

```rust
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // ← snapshot taken HERE
// ...
evicts = self.check_and_record_ancestors(&mut entry)?;          // ← may call remove_entry → update_stat_for_remove_tx
// ...
self.total_tx_size = total_tx_size;    // ← stale snapshot OVERWRITES live field
self.total_tx_cycles = total_tx_cycles;
``` [2](#0-1) 

`updated_stat_for_add_tx` reads `self.total_tx_size` at call time and adds the new entry's size: [3](#0-2) 

`remove_entry` (called by `check_and_record_ancestors` for each evicted tx) correctly decrements the live field via `update_stat_for_remove_tx`: [4](#0-3) [5](#0-4) 

Because the snapshot is taken before evictions and written back after them, every eviction's decrement is silently discarded. The final value of `total_tx_size` becomes:

```
original_total + new_entry_size          (correct: original_total + new_entry_size − Σ evicted_sizes)
```

The eviction path is explicitly documented in the code comment:

> "Currently, evicts when inserting is only due to referring cell dep will be consumed by this new transaction." [6](#0-5) 

---

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size` to enforce the pool's byte cap:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee-rate entries
}
``` [7](#0-6) 

An inflated `total_tx_size` causes `limit_size` to believe the pool is over-capacity when it is not, triggering cascading evictions of legitimate transactions. Evicted transactions are permanently rejected with `Reject::Full` and reported to the submitter as pool-full errors.

`total_tx_size` and `total_tx_cycles` are also surfaced directly to any RPC caller via `get_tx_pool_info`: [8](#0-7) 

Operators and tooling that rely on these values for fee estimation, pool monitoring, or admission decisions receive permanently incorrect data after any eviction-triggering insertion.

---

### Likelihood Explanation

The trigger condition — a new transaction that consumes a cell dep already referenced by an existing pool transaction — is reachable by any unprivileged `send_transaction` RPC caller or P2P transaction relay sender. No special privilege is required. The attacker only needs to craft a transaction whose `cell_deps` overlap with those of existing mempool transactions. Each such submission permanently inflates `total_tx_size` by the aggregate size of the evicted entries. Repeated submissions compound the drift without bound until the node restarts.

---

### Recommendation

Move the stat snapshot **after** `check_and_record_ancestors` completes, so it reflects the pool state post-eviction:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute totals AFTER evictions have already updated self.total_tx_*
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, accumulate the evicted sizes during `check_and_record_ancestors` and subtract them from the pre-computed snapshot before the final assignment.

---

### Proof of Concept

1. Submit transaction **T1** to the pool with `cell_dep = OutPoint(H, 0)`. It is accepted; `total_tx_size` = `S1`.
2. Submit transaction **T2** whose input consumes `OutPoint(H, 0)` (the same cell dep). `check_and_record_ancestors` evicts **T1** (size `S1`) via `remove_entry`, which decrements `self.total_tx_size` to `0`. Then `add_entry` writes back the stale snapshot `total_tx_size = 0 + S2 = S2 + S1` (where `S2` is T2's size), instead of the correct `S2`.
3. `total_tx_size` is now `S1 + S2` even though only T2 (size `S2`) is in the pool.
4. If `S1 + S2 > max_tx_pool_size`, `limit_size` immediately evicts T2 as well, leaving the pool empty while reporting it as over-capacity. Any subsequent `send_transaction` call is rejected with `Reject::Full` until the node restarts. [2](#0-1) [9](#0-8)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L68-74)
```rust
    // sum of all tx_pool tx's virtual sizes.
    pub(crate) total_tx_size: usize,
    // sum of all tx_pool tx's cycles.
    pub(crate) total_tx_cycles: Cycle,
    pub(crate) pending_count: usize,
    pub(crate) gap_count: usize,
    pub(crate) proposed_count: usize,
```

**File:** tx-pool/src/component/pool_map.rs (L197-221)
```rust
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

**File:** tx-pool/src/service.rs (L1083-1097)
```rust
        TxPoolInfo {
            tip_hash: tip_header.hash(),
            tip_number: tip_header.number(),
            pending_size: tx_pool.pool_map.pending_size(),
            proposed_size: tx_pool.pool_map.proposed_size(),
            orphan_size: orphan.len(),
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
            min_fee_rate: self.tx_pool_config.min_fee_rate,
            min_rbf_rate: self.tx_pool_config.min_rbf_rate,
            last_txs_updated_at: tx_pool.pool_map.get_max_update_time(),
            tx_size_limit: TRANSACTION_SIZE_LIMIT,
            max_tx_pool_size: self.tx_pool_config.max_tx_pool_size as u64,
            verify_queue_size: verify_queue.len(),
        }
```
