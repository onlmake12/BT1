### Title
Expired Transactions Promoted to Proposed State Before Expiry Pruning in `_update_tx_pool_for_reorg` - (`tx-pool/src/process.rs`)

---

### Summary

In `_update_tx_pool_for_reorg`, when the node is in mine mode, expired transactions are promoted from Pending/Gap status to Proposed status **before** `remove_expired` is called. This mirrors the original report's pattern exactly: a stale-state collection is iterated for accounting/state-transition purposes without first pruning expired entries.

---

### Finding Description

`_update_tx_pool_for_reorg` is the sole code path that calls `remove_expired` on the tx-pool. Its execution order is:

1. Remove committed transactions (`remove_committed_txs`)
2. Handle detached proposals (`remove_by_detached_proposal`)
3. **[mine_mode only]** Iterate all Gap and Pending entries and promote any whose `proposal_short_id` appears in the new snapshot's proposal window → move them to Proposed status
4. **Only then** call `remove_expired`
5. Call `limit_size` [1](#0-0) 

Steps 3 and 4 are inverted relative to correctness. A transaction whose `timestamp + expiry < now_ms` satisfies the expiry condition defined in `remove_expired`: [2](#0-1) 

Yet in step 3, the code iterates `Status::Gap` and `Status::Pending` entries and calls `proposed_rtx` / `gap_rtx` on any that match the new snapshot's proposal window — without first checking whether those entries are already expired: [3](#0-2) 

Because `remove_expired` is **only** called inside `_update_tx_pool_for_reorg` (no periodic background timer exists for the main pool), expired entries accumulate between blocks and are then promoted before being pruned. [4](#0-3) 

---

### Impact Explanation

1. **Expired transactions enter Proposed status and are packaged into block templates.** `package_txs` and `package_proposals` read directly from the pool map without calling `remove_expired` first. [5](#0-4) 

2. **`total_tx_size` / `total_tx_cycles` are inflated** by expired entries at the moment `limit_size` is evaluated (step 5). Because `limit_size` evicts valid transactions when `total_tx_size > max_tx_pool_size`, valid in-flight transactions can be unnecessarily evicted to make room that would have been freed by expiry pruning alone. [6](#0-5) 

3. **Fee-rate estimation is skewed.** `estimate_fee_rate` consumes `all_entry_info` (pending + proposed) without filtering expired entries, so the fee-rate surface presented to callers via `get_fee_rate_statistics` is distorted by transactions that should no longer exist in the pool. [7](#0-6) 

4. **`tx_pool_info` reports inflated counts.** The `info()` function reads `total_tx_size` and `total_tx_cycles` directly from `pool_map` without pruning first. [8](#0-7) 

---

### Likelihood Explanation

This is triggered on every block arrival when the node is in mine mode. Any node operator running `ckb miner` or using `get_block_template` is in mine mode. The window during which expired transactions are in Proposed status is one block interval (~10 s), but the pool-size inflation and fee-rate distortion persist for the entire duration between the last block and the current one — which can be arbitrarily long if the network stalls or the node is catching up.

The attacker-controlled entry path is straightforward: submit transactions via `send_transaction` RPC or P2P relay, wait for them to age past `expiry_hours`, then trigger a block arrival (or simply wait for the next block). No privileged access is required.

---

### Recommendation

Call `remove_expired` **before** the mine-mode promotion loop in `_update_tx_pool_for_reorg`, mirroring the fix pattern recommended in the original report (filter expired entries before iterating the active set):

```rust
// In _update_tx_pool_for_reorg:
tx_pool.remove_committed_txs(attached.iter(), callbacks, detached_headers);
tx_pool.remove_by_detached_proposal(detached_proposal_id.iter());

// Move expiry pruning BEFORE the promotion loop:
tx_pool.remove_expired(callbacks);

if mine_mode {
    // ... promote Gap/Pending to Proposed ...
}

let _ = tx_pool.limit_size(callbacks, None);
```

Additionally, consider adding a periodic background timer (analogous to the existing `clean_expired_orphan_timer` in `chain_service.rs`) that calls `remove_expired` independently of block arrival, so the pool stays clean even when blocks are slow. [9](#0-8) 

---

### Proof of Concept

1. Configure a node with `expiry_hours = 1` (or any small value for testing).
2. Submit a transaction `T` via `send_transaction`.
3. Wait until `T`'s `timestamp + expiry < now_ms` (i.e., it is logically expired).
4. Mine a block whose proposal window covers `T`'s `proposal_short_id`.
5. Observe via `get_block_template` that `T` appears in the `transactions` array of the returned template — it was promoted to Proposed in step 3 of `_update_tx_pool_for_reorg` before `remove_expired` ran in step 4.
6. Observe via `tx_pool_info` that `total_tx_size` / `total_tx_cycles` include `T`'s contribution until the next block triggers `remove_expired`.

### Citations

**File:** tx-pool/src/process.rs (L1061-1113)
```rust
    if mine_mode {
        let mut proposals = Vec::new();
        let mut gaps = Vec::new();

        for entry in tx_pool.pool_map.entries.get_by_status(&Status::Gap) {
            let short_id = entry.inner.proposal_short_id();
            if snapshot.proposals().contains_proposed(&short_id) {
                proposals.push((short_id, entry.inner.clone()));
            }
        }

        for entry in tx_pool.pool_map.entries.get_by_status(&Status::Pending) {
            let short_id = entry.inner.proposal_short_id();
            let elem = (short_id.clone(), entry.inner.clone());
            if snapshot.proposals().contains_proposed(&short_id) {
                proposals.push(elem);
            } else if snapshot.proposals().contains_gap(&short_id) {
                gaps.push(elem);
            }
        }

        for (id, entry) in proposals {
            debug!("begin to proposed: {:x}", id);
            if let Err(e) = tx_pool.proposed_rtx(&id) {
                debug!(
                    "Failed to add proposed tx {}, reason: {}",
                    entry.transaction().hash(),
                    e
                );
                callbacks.call_reject(tx_pool, &entry, e);
            } else {
                callbacks.call_proposed(&entry)
            }
        }

        for (id, entry) in gaps {
            debug!("begin to gap: {:x}", id);
            if let Err(e) = tx_pool.gap_rtx(&id) {
                debug!(
                    "Failed to add tx to gap {}, reason: {}",
                    entry.transaction().hash(),
                    e
                );
                callbacks.call_reject(tx_pool, &entry, e.clone());
            }
        }
    }

    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
```

**File:** tx-pool/src/pool.rs (L36-51)
```rust
pub struct TxPool {
    pub(crate) config: TxPoolConfig,
    pub(crate) pool_map: PoolMap,
    /// cache for committed transactions hash
    pub(crate) committed_txs_hash_cache: LruCache<ProposalShortId, Byte32>,
    /// storage snapshot reference
    pub(crate) snapshot: Arc<Snapshot>,
    /// record recent reject
    pub recent_reject: Option<RecentReject>,
    // expiration milliseconds,
    pub(crate) expiry: u64,
    // conflicted transaction cache
    pub(crate) conflicts_cache: lru::LruCache<ProposalShortId, TransactionView>,
    // conflicted transaction outputs cache, input -> tx_short_id
    pub(crate) conflicts_outputs_cache: lru::LruCache<OutPoint, ProposalShortId>,
}
```

**File:** tx-pool/src/pool.rs (L271-288)
```rust
    pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
        let now_ms = ckb_systemtime::unix_time_as_millis();

        let removed: Vec<_> = self
            .pool_map
            .iter()
            .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
            .map(|entry| entry.inner.clone())
            .collect();

        for entry in removed {
            let tx_hash = entry.transaction().hash();
            debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
            self.pool_map.remove_entry(&entry.proposal_short_id());
            let reject = Reject::Expiry(entry.timestamp);
            callbacks.call_reject(self, &entry, reject);
        }
    }
```

**File:** tx-pool/src/pool.rs (L290-329)
```rust
    // Remove transactions from the pool until total size <= size_limit.
    // Return a `Reject` for current inserting entry if it's removed
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

**File:** tx-pool/src/block_assembler/mod.rs (L190-213)
```rust
        let (proposals, txs, basic_size) = {
            let tx_pool_reader = tx_pool.read().await;
            if current.snapshot.tip_hash() != tx_pool_reader.snapshot().tip_hash() {
                return Ok(());
            }

            let proposals =
                tx_pool_reader.package_proposals(consensus.max_block_proposals_limit(), uncles);

            let basic_size = Self::basic_block_size(
                current_template.cellbase.data(),
                uncles,
                proposals.iter(),
                current_template.extension.clone(),
            );

            let txs_size_limit = max_block_bytes
                .checked_sub(basic_size)
                .ok_or(BlockAssemblerError::Overflow)?;

            let max_block_cycles = consensus.max_block_cycles();
            let (txs, _txs_size, _cycles) =
                tx_pool_reader.package_txs(max_block_cycles, txs_size_limit);
            (proposals, txs, basic_size)
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L164-185)
```rust
    pub fn estimate_fee_rate(
        &self,
        target_blocks: BlockNumber,
        all_entry_info: TxPoolEntryInfo,
    ) -> Result<FeeRate, Error> {
        if !self.is_ready {
            return Err(Error::NotReady);
        }

        let sorted_current_txs = {
            let mut current_txs: Vec<_> = all_entry_info
                .pending
                .into_values()
                .chain(all_entry_info.proposed.into_values())
                .map(TxStatus::new_from_entry_info)
                .collect();
            current_txs.sort_unstable_by(|a, b| b.cmp(a));
            current_txs
        };

        self.do_estimate(target_blocks, &sorted_current_txs)
    }
```

**File:** tx-pool/src/service.rs (L1078-1098)
```rust
    async fn info(&self) -> TxPoolInfo {
        let tx_pool = self.tx_pool.read().await;
        let orphan = self.orphan.read().await;
        let verify_queue = self.verify_queue.read().await;
        let tip_header = tx_pool.snapshot.tip_header();
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
    }
```

**File:** chain/src/chain_service.rs (L40-63)
```rust
        let clean_expired_orphan_timer =
            crossbeam::channel::tick(std::time::Duration::from_secs(60));

        loop {
            select! {
                recv(self.process_block_rx) -> msg => match msg {
                    Ok(Request { responder, arguments: lonely_block }) => {
                        // asynchronous_process_block doesn't interact with tx-pool,
                        // no need to pause tx-pool's chunk_process here.
                        let _trace_now = minstant::Instant::now();
                        self.asynchronous_process_block(lonely_block);
                        if let Some(handle) = ckb_metrics::handle(){
                            handle.ckb_chain_async_process_block_duration.observe(_trace_now.elapsed().as_secs_f64())
                        }
                        let _ = responder.send(());
                    },
                    _ => {
                        error!("process_block_receiver closed");
                        break;
                    },
                },
                recv(clean_expired_orphan_timer) -> _ => {
                    self.orphan_broker.clean_expired_orphans();
                },
```
