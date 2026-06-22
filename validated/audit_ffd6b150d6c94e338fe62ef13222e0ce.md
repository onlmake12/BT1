### Title
Tx-Pool `total_tx_size` / `total_tx_cycles` Accounting Corruption via Eviction During `add_entry` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new aggregate pool statistics (`total_tx_size`, `total_tx_cycles`) are computed **before** ancestor-evictions occur, then written back **after** those evictions have already decremented the running totals. This causes the pool's size/cycle counters to be permanently inflated by the sum of all evicted entries, mirroring the `totalDeposits` / `totalActiveStakeAmount` accounting corruption described in the external report.

---

### Finding Description

`PoolMap::add_entry` in `tx-pool/src/component/pool_map.rs` follows this sequence:

```
(1) total_tx_size_new = self.total_tx_size + entry.size   // pre-compute
(2) check_and_record_ancestors()                           // may evict N entries,
    └─ remove_entry_and_descendants()                      //   each calling
       └─ remove_entry()                                   //   update_stat_for_remove_tx()
          └─ self.total_tx_size -= evicted.size            //   decrement in-place
(3) self.total_tx_size = total_tx_size_new                 // OVERWRITES decrements
``` [1](#0-0) 

At step (1), `updated_stat_for_add_tx` captures the current `self.total_tx_size` and adds the new entry's size into a local variable. [2](#0-1) 

At step (2), `check_and_record_ancestors` may call `remove_entry_and_descendants` to evict cell-ref-parent transactions when the ancestor count exceeds `max_ancestors_count`. Each eviction calls `update_stat_for_remove_tx`, which decrements `self.total_tx_size` and `self.total_tx_cycles` in-place. [3](#0-2) [4](#0-3) 

At step (3), the pre-computed local `total_tx_size_new` (which was computed before any evictions) is written back to `self.total_tx_size`, silently discarding all the decrements applied during eviction. [5](#0-4) 

The net result: after the call, `self.total_tx_size` equals `(pre-eviction total) + (new entry size)` instead of the correct `(pre-eviction total) − (sum of evicted sizes) + (new entry size)`. The pool's size counter is permanently inflated by the total byte-size of all evicted transactions.

The same overwrite applies to `self.total_tx_cycles`.

---

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size` to decide whether to evict further transactions:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee-rate entries
}
``` [6](#0-5) 

An inflated counter causes `limit_size` to evict additional legitimate transactions that should remain in the pool. Repeated triggering accumulates inflation, eventually making the pool appear full (`total_tx_size > max_tx_pool_size`) even when it holds far fewer bytes than the configured limit. Subsequent `send_transaction` calls are then rejected with `Reject::Full`, constituting a local tx-pool DoS. The `total_tx_cycles` inflation similarly distorts fee-estimation and pool-info RPC responses.

---

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged `send_transaction` RPC caller or relay peer. The attacker:

1. Submits many transactions that reference a specific live cell as a `cell_dep` (creating a large set of `cell_ref_parents` in the pool).
2. Submits a transaction that **spends** that same cell as an input. This triggers the ancestor-count overflow branch, causing `remove_entry_and_descendants` to be called for each evicted cell-ref-parent.
3. Each such submission inflates `total_tx_size` by the sizes of the evicted entries.

No privileged access, key material, or majority hashpower is required. The `send_transaction` RPC and the relay protocol (`RelayV3`) are both open to unprivileged callers. [7](#0-6) [8](#0-7) 

---

### Recommendation

Recompute or re-read `self.total_tx_size` and `self.total_tx_cycles` **after** `check_and_record_ancestors` completes (i.e., after all evictions have been applied), then add only the new entry's contribution:

```rust
// After check_and_record_ancestors, evictions are already reflected in self.total_tx_*
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
// ... record edges, insert, record descendants ...
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Move the `updated_stat_for_add_tx` call to after `check_and_record_ancestors` so it reads the post-eviction totals, or alternatively accumulate the evicted sizes/cycles and subtract them from the pre-computed value before writing back.

---

### Proof of Concept

**Setup** (all via `send_transaction` RPC, no privileges needed):

1. Mine enough blocks to have a mature cellbase output `C`.
2. Submit `tx_root` spending `C`, producing output `cell_dep_out` (the shared cell dep).
3. Submit `N > max_ancestors_count` transactions `ref_tx_1 … ref_tx_N`, each spending an independent live cell and including `cell_dep_out` as a `cell_dep`. These become `cell_ref_parents` in the pool.
4. Submit `tx_consume` that spends `cell_dep_out` as an **input**. Its ancestor count is `N + 1 > max_ancestors_count`, so `check_and_record_ancestors` enters the eviction branch and removes `N − max_ancestors_count + 1` of the `ref_tx_*` entries.
5. Query `tx_pool_info`. Observe `total_tx_size` is larger than the sum of sizes of all currently-pooled transactions.
6. Repeat steps 3–4 to accumulate inflation until `total_tx_size > max_tx_pool_size`.
7. Attempt to submit any valid transaction; it is rejected with `PoolIsFull` even though the pool is nearly empty. [1](#0-0) [3](#0-2) [6](#0-5)

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

**File:** tx-pool/src/component/pool_map.rs (L588-640)
```rust
    fn check_and_record_ancestors(
        &mut self,
        entry: &mut TxEntry,
    ) -> Result<HashSet<TxEntry>, Reject> {
        let tx = entry.transaction();
        let (ancestors, mut parents, cell_ref_parents) = self.get_tx_ancenstors(tx);

        let mut ancestors_count = ancestors.len() + 1;
        let mut evicted = Default::default();

        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
        }

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
        } else {
            return Err(Reject::ExceededMaximumAncestorsCount);
        }

        // some txs in `parents` are removed, now `ancestors` need to re-caculate,
        let ancestors = self
            .links
            .calc_relation_ids(parents.clone(), Relation::Parents);

        // we can assume the number now is less than `max_ancestors_count`
        assert!(ancestors.len() < self.max_ancestors_count);

        self._record_ancestors(entry, ancestors, parents);
        Ok(evicted)
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

**File:** tx-pool/src/process.rs (L335-353)
```rust
    pub(crate) async fn resumeble_process_tx(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
        self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
    }
```
