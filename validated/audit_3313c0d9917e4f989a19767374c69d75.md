### Title
`readd_detached_tx` Bypasses `max_tx_pool_size` Invariant During Chain Reorg, Causing Pool Size Violation and Legitimate Transaction Eviction - (File: `tx-pool/src/process.rs`)

### Summary

During a chain reorganization, `update_tx_pool_for_reorg` first enforces the `max_tx_pool_size` limit via `_update_tx_pool_for_reorg`, then immediately re-adds detached block transactions via `readd_detached_tx` — without calling `limit_size` afterwards. This allows the tx-pool to silently exceed its configured size bound. When the next user transaction is submitted via the normal path, `limit_size` is triggered and evicts the lowest-fee-rate transactions, which may include legitimate in-pool transactions that were present before the reorg.

### Finding Description

The `max_tx_pool_size` invariant is enforced in two places:

1. **Normal submission path** (`submit_entry`, line 151): after `_submit_entry` adds a transaction, `limit_size` is called immediately.
2. **Reorg path** (`_update_tx_pool_for_reorg`, line 1113): `limit_size` is called at the end of the function.

The problem is in `update_tx_pool_for_reorg`:

```
_update_tx_pool_for_reorg(...)   // ends with limit_size → pool ≤ max_tx_pool_size
readd_detached_tx(...)           // adds txs via _submit_entry → NO limit_size call
``` [1](#0-0) 

`_update_tx_pool_for_reorg` calls `limit_size` at its very end: [2](#0-1) 

Then `readd_detached_tx` is called, which calls `_submit_entry` for each detached transaction: [3](#0-2) 

`_submit_entry` calls `add_pending`/`add_gap`/`add_proposed`, all of which increment `pool_map.total_tx_size` with no subsequent `limit_size` call: [4](#0-3) 

Contrast with the normal `submit_entry` path, which always calls `limit_size` after insertion: [5](#0-4) 

### Impact Explanation

After a reorg, the pool can exceed `max_tx_pool_size` by up to the total serialized size of all re-added detached transactions. The pool remains over the limit until the next call to `submit_entry`. At that point, `limit_size` evicts the lowest-fee-rate transactions — which may include legitimate user transactions that were in the pool before the reorg, not the newly re-added ones. This violates the `max_tx_pool_size` resource accounting invariant and causes unpredictable eviction of user transactions. [6](#0-5) 

### Likelihood Explanation

Chain reorganizations are a normal part of CKB operation. A 1-block reorg occurs naturally whenever two miners find blocks at the same height simultaneously. Any miner — even one with a small fraction of hashpower — can produce a competing block. No privileged access, leaked keys, or majority hashpower is required. The trigger is a standard protocol event reachable by any network participant who mines a valid block.

### Recommendation

Call `tx_pool.limit_size(&self.callbacks, None)` at the end of `readd_detached_tx`, or immediately after it returns in `update_tx_pool_for_reorg`, to restore the `max_tx_pool_size` invariant after all detached transactions have been re-added.

### Proof of Concept

1. Node A has a full tx-pool at exactly `max_tx_pool_size` (e.g., 180 MB).
2. A 1-block reorg occurs: block B1 is detached, block B2 (same height) is attached. B1 contains N transactions not in B2.
3. `update_tx_pool_for_reorg` is called:
   - `_update_tx_pool_for_reorg` runs → `limit_size` enforces the 180 MB cap → pool = 180 MB.
   - `readd_detached_tx` runs → N transactions from B1 are re-added via `_submit_entry` → pool = 180 MB + size(B1 txs), with no `limit_size` call.
4. Pool now exceeds `max_tx_pool_size`. The next `send_transaction` RPC call triggers `submit_entry` → `limit_size` evicts the lowest-fee-rate transactions (potentially legitimate pre-reorg transactions) to restore the cap. [7](#0-6) [8](#0-7)

### Citations

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

**File:** tx-pool/src/process.rs (L802-858)
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
    }
```

**File:** tx-pool/src/process.rs (L878-914)
```rust
    async fn readd_detached_tx(
        &self,
        tx_pool: &mut TxPool,
        txs: Vec<TransactionView>,
        fetched_cache: HashMap<Byte32, CacheEntry>,
    ) {
        let max_cycles = self.tx_pool_config.max_tx_verify_cycles;
        for tx in txs {
            let tx_size = tx.data().serialized_size_in_block();
            let tx_hash = tx.hash();
            if let Ok((rtx, status)) = resolve_tx(tx_pool, tx_pool.snapshot(), tx, false)
                && let Ok(fee) = check_tx_fee(tx_pool, tx_pool.snapshot(), &rtx, tx_size)
            {
                let verify_cache = fetched_cache.get(&tx_hash).cloned();
                let snapshot = tx_pool.cloned_snapshot();
                let tip_header = snapshot.tip_header();
                let tx_env = Arc::new(status.with_env(tip_header));
                if let Ok(verified) = verify_rtx(
                    snapshot,
                    Arc::clone(&rtx),
                    tx_env,
                    &verify_cache,
                    max_cycles,
                    None,
                )
                .await
                {
                    let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
                    if let Err(e) = _submit_entry(tx_pool, status, entry, &self.callbacks) {
                        error!("readd_detached_tx submit_entry {} error {}", tx_hash, e);
                    } else {
                        debug!("readd_detached_tx submit_entry {}", tx_hash);
                    }
                }
            }
        }
    }
```

**File:** tx-pool/src/process.rs (L1016-1037)
```rust
fn _submit_entry(
    tx_pool: &mut TxPool,
    status: TxStatus,
    entry: TxEntry,
    callbacks: &Callbacks,
) -> Result<HashSet<TxEntry>, Reject> {
    let tx_hash = entry.transaction().hash();
    debug!("submit_entry {:?} {}", status, tx_hash);
    let (succ, evicts) = match status {
        TxStatus::Fresh => tx_pool.add_pending(entry.clone())?,
        TxStatus::Gap => tx_pool.add_gap(entry.clone())?,
        TxStatus::Proposed => tx_pool.add_proposed(entry.clone())?,
    };
    if succ {
        match status {
            TxStatus::Fresh => callbacks.call_pending(&entry),
            TxStatus::Gap => callbacks.call_pending(&entry),
            TxStatus::Proposed => callbacks.call_proposed(&entry),
        }
    }
    Ok(evicts)
}
```

**File:** tx-pool/src/process.rs (L1109-1113)
```rust
    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
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
