### Title
Pool Size Limit (`max_tx_pool_size`) Bypassed During Chain Reorg via `readd_detached_tx` — (File: `tx-pool/src/process.rs`)

---

### Summary

During a chain reorganization, detached transactions are re-added to the tx-pool via `readd_detached_tx`. This path calls the internal `_submit_entry` free function directly, **without** the subsequent `limit_size` call that the normal submission path enforces. As a result, the configured `max_tx_pool_size` invariant can be permanently violated until the next normal transaction submission triggers eviction.

---

### Finding Description

There are two distinct code paths that insert entries into the tx-pool:

**Path 1 — Normal submission** (`submit_entry` method, `tx-pool/src/process.rs` lines 96–170):

```rust
let evicted = _submit_entry(tx_pool, status, entry.clone(), &self.callbacks)?;
// ...
tx_pool
    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
    .map_or(Ok(()), Err)?;
```

After inserting via `_submit_entry`, `limit_size` is called to evict low-fee-rate transactions until `total_tx_size <= max_tx_pool_size`.

**Path 2 — Reorg re-add** (`readd_detached_tx`, `tx-pool/src/process.rs` lines 878–914):

```rust
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
if let Err(e) = _submit_entry(tx_pool, status, entry, &self.callbacks) {
    error!("readd_detached_tx submit_entry {} error {}", tx_hash, e);
} else {
    debug!("readd_detached_tx submit_entry {}", tx_hash);
}
```

`_submit_entry` is called directly with **no subsequent `limit_size` call**. Every detached transaction that passes `check_tx_fee` and `verify_rtx` is unconditionally inserted into the pool, regardless of whether the pool is already at or above `max_tx_pool_size`.

The `limit_size` function itself (`tx-pool/src/pool.rs` lines 292–329) is the sole enforcement point for the pool-size invariant:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee-rate entries
}
```

It is never called from `readd_detached_tx`.

---

### Impact Explanation

After a chain reorganization, all transactions from detached blocks that are still valid are re-inserted into the pool without any size-limit enforcement. If the pool was near `max_tx_pool_size` before the reorg, or if the detached blocks contained many large transactions, the pool's `total_tx_size` can grow arbitrarily beyond the configured limit. The violation persists in memory until the next externally submitted transaction triggers `limit_size` via the normal path. In a quiescent network (no new transactions arriving), the pool remains over-limit indefinitely, consuming memory beyond the operator-configured bound.

---

### Likelihood Explanation

One-block reorgs are a routine occurrence in any PoW network and require no attacker involvement. Any peer acting as a block relayer can send a valid competing block at the same height, causing the local node to reorganize and invoke `update_tx_pool_for_reorg` → `readd_detached_tx`. The attacker does not need majority hashpower; a single valid competing block suffices to trigger the bypass. The scenario is therefore reachable by any unprivileged block-relayer peer.

---

### Recommendation

After the loop in `readd_detached_tx` completes, call `limit_size` on the pool to restore the invariant:

```rust
// after the for-loop in readd_detached_tx
tx_pool.limit_size(&self.callbacks, None);
```

Alternatively, refactor `readd_detached_tx` to call the full `submit_entry` method (which already includes the `limit_size` call) instead of the bare `_submit_entry` free function, consistent with how the normal submission path works.

---

### Proof of Concept

**Normal path** — `submit_entry` in `tx-pool/src/process.rs`: [1](#0-0) 

**Reorg path** — `readd_detached_tx` in `tx-pool/src/process.rs` (no `limit_size` call): [2](#0-1) 

**The enforcement function that is skipped** — `limit_size` in `tx-pool/src/pool.rs`: [3](#0-2) 

**Reorg dispatch** — `update_tx_pool_for_reorg` calls `readd_detached_tx` inside the write-lock scope: [4](#0-3)

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
