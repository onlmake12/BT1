### Title
Unbounded Iteration Over All Tx-Pool Entries on Every Block Acceptance Enables Sustained CPU Exhaustion — (`tx-pool/src/pool.rs`, `tx-pool/src/process.rs`)

### Summary
On every accepted block, `_update_tx_pool_for_reorg` unconditionally iterates over **all** `Pending` and `Gap` entries in the tx-pool, and `remove_expired` iterates over **all** entries regardless of pool size. An unprivileged attacker who fills the tx-pool with many small transactions can force O(N) CPU work on the node for every block relayed by any peer, causing sustained processing degradation proportional to pool occupancy.

### Finding Description

**Root cause 1 — `remove_expired` (`tx-pool/src/pool.rs:271-288`)**

```rust
pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
    let now_ms = ckb_systemtime::unix_time_as_millis();
    let removed: Vec<_> = self
        .pool_map
        .iter()          // ← iterates ALL entries, no limit
        .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
        .map(|entry| entry.inner.clone())
        .collect();
    for entry in removed { ... }
}
```

`pool_map.iter()` walks every entry in the pool. There is no early-exit or per-call cap.

**Root cause 2 — `_update_tx_pool_for_reorg` (`tx-pool/src/process.rs:1065-1080`)**

```rust
for entry in tx_pool.pool_map.entries.get_by_status(&Status::Gap) { ... }
for entry in tx_pool.pool_map.entries.get_by_status(&Status::Pending) { ... }
```

Both loops scan the entire pool unconditionally. This function is called synchronously inside the tx-pool write-lock on every block.

**Call chain (triggered by any block relay):**

```
P2P peer relays block
  → Relayer::accept_block (sync/src/relayer/mod.rs:274)
    → ChainController::process_block
      → TxPoolController::update_tx_pool_for_reorg (tx-pool/src/service.rs:241)
        → process::update_tx_pool_for_reorg (tx-pool/src/process.rs:802)
          → _update_tx_pool_for_reorg (tx-pool/src/process.rs:1039)
            → tx_pool.remove_committed_txs(...)
            → tx_pool.remove_by_detached_proposal(...)
            → for entry in get_by_status(Gap)   ← unbounded
            → for entry in get_by_status(Pending) ← unbounded
            → tx_pool.remove_expired(...)        ← unbounded
```

**Attacker setup:**
1. Submit many small, valid, low-fee transactions via `send_transaction` RPC or P2P relay to fill the pool up to `max_tx_pool_size` (default 180 MB). With minimum-viable transactions (~200 bytes each), this yields ~900,000 pool entries.
2. The pool's `limit_size` eviction only runs *after* the unbounded scans complete, so the full O(N) cost is paid on every block.
3. Every ~10 seconds a new block arrives, re-triggering all three unbounded loops while the pool remains full.

### Impact Explanation

Every block acceptance holds the tx-pool write-lock while iterating potentially hundreds of thousands of entries. This:
- Delays tx-pool operations (submission, RPC queries) for the duration of the lock
- Causes sustained CPU load proportional to pool size on every block
- Degrades block-template generation (`get_block_template`) which also acquires the pool read-lock

**Severity: Medium** — does not cause consensus failure or fund loss, but causes measurable, sustained service degradation on any node with a full pool. The attacker only needs to keep the pool full (cheap, low-fee txs are sufficient if the node's min-fee-rate is low or zero in test configurations).

### Likelihood Explanation

**Medium.** The attacker needs no privileged access — only the ability to submit transactions (standard P2P or RPC). Keeping the pool full requires ongoing fee expenditure, but with a low configured `min_fee_rate` the cost is minimal. The trigger (block relay) fires automatically every ~10 seconds.

### Recommendation

1. **Cap the per-block scan in `_update_tx_pool_for_reorg`**: process at most `MAX_REORG_SCAN_ENTRIES` entries per call, deferring the rest.
2. **Use a time-indexed structure for `remove_expired`**: maintain a sorted-by-timestamp index so only entries near the expiry boundary are scanned, rather than the entire pool.
3. **Release the write-lock between batches** or move the status-transition loops to an async task that yields periodically.
4. **Enforce a tighter pool entry count limit** (not just byte size) to bound worst-case iteration depth.

### Proof of Concept

```
1. Configure a CKB node with default max_tx_pool_size (180 MB).
2. Submit ~50,000 valid transactions (each ~3600 bytes, just above min size)
   via repeated send_transaction RPC calls. Each tx pays minimum fee.
3. Observe pool fills to capacity (limit_size evicts lowest-fee txs).
4. Mine or relay a new block to any peer connected to the target node.
5. Measure: the tx-pool write-lock is held for the duration of:
     - remove_committed_txs (O(block_txs))
     - get_by_status(Gap) scan  → O(N_gap)
     - get_by_status(Pending) scan → O(N_pending)
     - remove_expired scan → O(N_total)
   With 50,000 entries, each block triggers ~150,000 total entry visits
   under the write-lock, repeating every ~10 seconds.
6. Concurrent send_transaction or get_block_template calls stall for the
   lock duration, confirming service degradation.
```

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

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

**File:** tx-pool/src/process.rs (L802-857)
```rust
    pub(crate) async fn update_tx_pool_for_reorg(
        &self,
        detached_blocks: VecDeque<BlockView>,
        attached_blocks: VecDeque<BlockView>,
        detached_proposal_id: HashSet<ProposalShortId>,
        snapshot: Arc<Snapshot>,
    ) {
        let mine_mode = self.block_assembler.is_some();
        let mut detached = LinkedHashSet::default();
        let mut attached = LinkedHashSet::default();

        let detached_headers: HashSet<Byte32> = detached_blocks
            .iter()
            .map(|blk| blk.header().hash())
            .collect();

        for blk in detached_blocks {
            detached.extend(blk.transactions().into_iter().skip(1))
        }

        for blk in attached_blocks {
            self.fee_estimator.commit_block(&blk);
            attached.extend(blk.transactions().into_iter().skip(1));
        }
        let retain: Vec<TransactionView> = detached.difference(&attached).cloned().collect();

        let fetched_cache = self.fetch_txs_verify_cache(retain.iter()).await;

        // If there are any transactions requires re-process, return them.
        //
        // At present, there is only one situation:
        // - If the hardfork was happened, then re-process all transactions.
        {
            // This closure is used to limit the lifetime of mutable tx_pool.
            let mut tx_pool = self.tx_pool.write().await;

            _update_tx_pool_for_reorg(
                &mut tx_pool,
                &attached,
                &detached_headers,
                detached_proposal_id,
                snapshot,
                &self.callbacks,
                mine_mode,
            );

            // notice: readd_detached_tx don't update cache
            self.readd_detached_tx(&mut tx_pool, retain, fetched_cache)
                .await;
        }

        self.remove_orphan_txs_by_attach(&attached).await;
        {
            let mut queue = self.verify_queue.write().await;
            queue.remove_txs(attached.iter().map(|tx| tx.proposal_short_id()));
        }
```

**File:** tx-pool/src/process.rs (L1039-1056)
```rust
fn _update_tx_pool_for_reorg(
    tx_pool: &mut TxPool,
    attached: &LinkedHashSet<TransactionView>,
    detached_headers: &HashSet<Byte32>,
    detached_proposal_id: HashSet<ProposalShortId>,
    snapshot: Arc<Snapshot>,
    callbacks: &Callbacks,
    mine_mode: bool,
) {
    tx_pool.snapshot = Arc::clone(&snapshot);

    // NOTE: `remove_by_detached_proposal` will try to re-put the given expired/detached proposals into
    // pending-pool if they can be found within txpool. As for a transaction
    // which is both expired and committed at the one time(commit at its end of commit-window),
    // we should treat it as a committed and not re-put into pending-pool. So we should ensure
    // that involves `remove_committed_txs` before `remove_expired`.
    tx_pool.remove_committed_txs(attached.iter(), callbacks, detached_headers);
    tx_pool.remove_by_detached_proposal(detached_proposal_id.iter());
```

**File:** tx-pool/src/process.rs (L1065-1080)
```rust
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
```

**File:** sync/src/relayer/mod.rs (L274-343)
```rust
    pub fn accept_block(
        &self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_id: PeerIndex,
        block: core::BlockView,
        msg_name: &str,
    ) {
        if self
            .shared()
            .active_chain()
            .contains_block_status(&block.hash(), BlockStatus::BLOCK_STORED)
        {
            return;
        }

        let block = Arc::new(block);

        let verify_callback = {
            let nc: Arc<dyn CKBProtocolContext + Sync> = Arc::clone(&nc);
            let block = Arc::clone(&block);
            let shared = Arc::clone(self.shared());
            let msg_name = msg_name.to_owned();
            Box::new(move |result: VerifyResult| match result {
                Ok(verified) => {
                    if !verified {
                        debug!(
                            "block {}-{} has verified already, won't build compact block and broadcast it",
                            block.number(),
                            block.hash()
                        );
                        return;
                    }

                    build_and_broadcast_compact_block(nc.as_ref(), shared.shared(), peer_id, block);
                }
                Err(err) => {
                    error!(
                        "verify block {}-{} failed: {:?}, won't build compact block and broadcast it",
                        block.number(),
                        block.hash(),
                        err
                    );

                    let is_internal_db_error = is_internal_db_error(&err);
                    if is_internal_db_error {
                        return;
                    }

                    // punish the malicious peer
                    post_sync_process(
                        nc.as_ref(),
                        peer_id,
                        &msg_name,
                        StatusCode::BlockIsInvalid.with_context(format!(
                            "block {} is invalid, reason: {}",
                            block.hash(),
                            err
                        )),
                    );
                }
            })
        };

        let remote_block = RemoteBlock {
            block,
            verify_callback,
        };

        self.shared.accept_remote_block(&self.chain, remote_block);
    }
```
