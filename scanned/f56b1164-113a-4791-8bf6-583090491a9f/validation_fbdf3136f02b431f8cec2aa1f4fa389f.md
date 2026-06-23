### Title
Pool Size Limit Not Enforced After Re-Adding Detached Transactions During Reorg - (`tx-pool/src/process.rs`)

### Summary

During a chain reorganization, detached transactions are re-added to the tx-pool via `readd_detached_tx`. Unlike the normal transaction submission path, this code path calls `_submit_entry` but **never calls `limit_size`** afterward. As a result, the `max_tx_pool_size` invariant is not enforced after the re-insertion, allowing the pool to silently grow beyond its configured limit.

### Finding Description

The normal transaction submission path in `submit_entry` correctly enforces the pool size limit after inserting a transaction:

```rust
// tx-pool/src/process.rs (submit_entry, lines 137–152)
let evicted = _submit_entry(tx_pool, status, entry.clone(), &self.callbacks)?;
// ...
tx_pool
    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
    .map_or(Ok(()), Err)?;
``` [1](#0-0) 

However, `readd_detached_tx` — which re-adds transactions from detached blocks during a reorg — calls `_submit_entry` without any subsequent `limit_size` call:

```rust
// tx-pool/src/process.rs (readd_detached_tx, lines 905–911)
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
if let Err(e) = _submit_entry(tx_pool, status, entry, &self.callbacks) {
    error!("readd_detached_tx submit_entry {} error {}", tx_hash, e);
} else {
    debug!("readd_detached_tx submit_entry {}", tx_hash);
}
``` [2](#0-1) 

The `limit_size` function is the sole enforcement point for `max_tx_pool_size`:

```rust
// tx-pool/src/pool.rs (limit_size, lines 292–329)
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee-rate entries
}
``` [3](#0-2) 

`readd_detached_tx` is called unconditionally from `update_tx_pool_for_reorg` for every transaction in every detached block: [4](#0-3) 

The `max_tx_pool_size` configuration (default 180 MB) is the primary resource-accounting guard for the mempool: [5](#0-4) 

### Impact Explanation

After a reorg, all transactions from detached blocks that pass verification are re-inserted into the pool with no size-limit enforcement. If the pool was already near its limit before the reorg, the re-insertion of even one block's worth of transactions (up to the consensus `max_block_bytes` ≈ 597 KB) can push `total_tx_size` above `max_tx_pool_size`. The pool then operates in an over-limit state until the next normally submitted transaction triggers `limit_size`. In a deep reorg (multiple detached blocks), the overshoot is proportionally larger. This causes:

1. **Memory over-commitment**: The pool holds more data than its configured limit, violating the operator's resource budget.
2. **Incorrect eviction decisions**: Subsequent calls to `limit_size` will evict transactions based on a `total_tx_size` that was inflated by the un-limited re-insertions, potentially ejecting legitimate high-fee transactions unnecessarily.

### Likelihood Explanation

Single-block reorgs are a routine network event on any proof-of-work chain and require no attacker involvement. Every natural reorg triggers `readd_detached_tx`. The vulnerability is therefore reachable by any network participant who submits transactions that end up in a detached block — a condition that occurs without any special privilege.

### Recommendation

Call `limit_size` after the loop in `readd_detached_tx`, mirroring the pattern in `submit_entry`:

```rust
async fn readd_detached_tx(&self, tx_pool: &mut TxPool, txs: Vec<TransactionView>, ...) {
    for tx in txs {
        // ... existing verify + _submit_entry logic ...
    }
    // Enforce pool size limit after all re-insertions
    tx_pool.limit_size(&self.callbacks, None);
}
```

Alternatively, call `limit_size` inside the loop after each `_submit_entry` call, consistent with the normal submission path.

### Proof of Concept

1. Fill the tx-pool to near `max_tx_pool_size` (e.g., 179 MB of pending transactions).
2. Mine a block that includes some of those transactions, then trigger a 1-block reorg (a competing block at the same height is accepted as the new tip).
3. `update_tx_pool_for_reorg` calls `readd_detached_tx` for all transactions in the detached block.
4. Each call to `_submit_entry` increases `pool_map.total_tx_size` without any `limit_size` call.
5. After the reorg, `pool_map.total_tx_size` exceeds `max_tx_pool_size` with no eviction having occurred.
6. Observe via `get_tx_pool_info` RPC that `total_tx_size` reports a value above the configured `max_tx_pool_size`. [6](#0-5)

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

**File:** util/app-config/src/configs/tx_pool.rs (L11-14)
```rust
pub struct TxPoolConfig {
    /// Keep the transaction pool below <max_tx_pool_size> mb
    pub max_tx_pool_size: usize,
    /// txs with lower fee rate than this will not be relayed or be mined
```
