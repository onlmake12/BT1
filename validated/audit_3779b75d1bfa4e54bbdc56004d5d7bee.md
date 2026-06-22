### Title
Tx-Pool Size Limit Bypassed During Reorg Re-add Path — (`File: tx-pool/src/process.rs`)

---

### Summary

The CKB tx-pool enforces a `max_tx_pool_size` limit on every normal transaction submission via `limit_size`. However, the `readd_detached_tx` path — which re-inserts transactions from detached blocks back into the pool after a chain reorganization — calls `_submit_entry` directly without invoking `limit_size` afterward. Because `readd_detached_tx` executes **after** `_update_tx_pool_for_reorg` (which does call `limit_size`), the pool can grow arbitrarily beyond its configured size limit during any reorg.

---

### Finding Description

The tx-pool enforces its size limit in two places:

**Normal submission path** (`submit_entry`, `tx-pool/src/process.rs` lines 96–169):

```rust
tx_pool
    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
    .map_or(Ok(()), Err)?;
```

**Reorg cleanup path** (`_update_tx_pool_for_reorg`, `tx-pool/src/process.rs` line 1113):

```rust
// Remove transactions from the pool until its size <= size_limit.
let _ = tx_pool.limit_size(callbacks, None);
```

However, `readd_detached_tx` (`tx-pool/src/process.rs` lines 878–914) — which runs **after** `_update_tx_pool_for_reorg` — calls `_submit_entry` in a loop for every detached transaction without ever calling `limit_size`:

```rust
if let Err(e) = _submit_entry(tx_pool, status, entry, &self.callbacks) {
    error!("readd_detached_tx submit_entry {} error {}", tx_hash, e);
} else {
    debug!("readd_detached_tx submit_entry {}", tx_hash);
}
// ← no limit_size call here
```

The call sequence in `update_tx_pool_for_reorg` (lines 836–851) is:

```rust
_update_tx_pool_for_reorg(   // ← calls limit_size at the end
    &mut tx_pool, ...
);
// notice: readd_detached_tx don't update cache
self.readd_detached_tx(&mut tx_pool, retain, fetched_cache)  // ← no limit_size
    .await;
```

After `_update_tx_pool_for_reorg` enforces the size limit, `readd_detached_tx` re-adds all detached transactions without re-checking the limit, leaving `pool_map.total_tx_size` potentially far above `config.max_tx_pool_size`.

---

### Impact Explanation

The `max_tx_pool_size` limit is the primary memory-bounding mechanism for the tx-pool. Bypassing it during reorgs allows `pool_map.total_tx_size` to grow unboundedly relative to the configured limit. In a reorg involving many large transactions (up to `TRANSACTION_SIZE_LIMIT = 512 KB` each), the pool can consume significantly more memory than the operator configured. This can lead to:

- Unbounded heap growth in the tx-pool process, potentially causing OOM kills on resource-constrained nodes.
- Violation of the operator's resource accounting invariant: the pool size limit is no longer faithfully maintained after any reorg.
- Degraded node performance due to oversized pool iteration during block assembly and eviction.

---

### Likelihood Explanation

Reorgs are a normal part of CKB network operation and occur without any attacker involvement whenever two miners find blocks at the same height. Any sync peer or block relayer can deliver a valid competing chain that triggers a reorg. The `update_tx_pool_for_reorg` path is exercised on every reorg, making this bypass reachable by any unprivileged peer who relays a valid block. A miner with even modest hashpower can deliberately trigger reorgs to amplify the effect.

---

### Recommendation

After `readd_detached_tx` completes, call `limit_size` on the tx-pool to enforce the size bound, mirroring the pattern already used in `_update_tx_pool_for_reorg`:

```rust
self.readd_detached_tx(&mut tx_pool, retain, fetched_cache).await;
// Enforce size limit after re-adding detached transactions
let _ = tx_pool.limit_size(&self.callbacks, None);
```

Alternatively, call `limit_size` inside `readd_detached_tx` after each successful `_submit_entry` call, consistent with the normal `submit_entry` path.

---

### Proof of Concept

1. Configure a CKB node with a small `max_tx_pool_size` (e.g., 2 MB).
2. Submit enough large transactions (each near `TRANSACTION_SIZE_LIMIT = 512 KB`) to fill the pool to its limit. Observe that `get_tx_pool_info` reports `total_tx_size ≈ max_tx_pool_size`.
3. Mine a block that includes those transactions, then trigger a reorg by presenting a competing chain of equal or greater length that does not include those transactions (e.g., via a second node mining a fork).
4. The chain service calls `update_tx_pool_for_reorg`. `_update_tx_pool_for_reorg` runs and calls `limit_size`, bringing the pool to ≤ `max_tx_pool_size`. Then `readd_detached_tx` re-adds all the detached transactions without calling `limit_size`.
5. Observe via `get_tx_pool_info` that `total_tx_size` now exceeds `max_tx_pool_size` by the size of the re-added transactions, violating the configured limit.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** tx-pool/src/process.rs (L96-152)
```rust
    pub(crate) async fn submit_entry(
        &self,
        pre_resolve_tip: Byte32,
        entry: TxEntry,
        mut status: TxStatus,
    ) -> (Result<(), Reject>, Arc<Snapshot>) {
        let (ret, snapshot) = self
            .with_tx_pool_write_lock(move |tx_pool, snapshot| {
                // check_rbf must be invoked in `write` lock to avoid concurrent issues.
                let conflicts = if tx_pool.enable_rbf() {
                    tx_pool.check_rbf(&snapshot, &entry)?
                } else {
                    // RBF is disabled but we found conflicts, return error here
                    // after_process will put this tx into conflicts_pool
                    let conflicted_outpoint =
                        tx_pool.pool_map.find_conflict_outpoint(entry.transaction());
                    if let Some(outpoint) = conflicted_outpoint {
                        return Err(Reject::Resolve(OutPointError::Dead(outpoint)));
                    }
                    HashSet::new()
                };

                // if snapshot changed by context switch we need redo time_relative verify
                let tip_hash = snapshot.tip_hash();
                if pre_resolve_tip != tip_hash {
                    debug!(
                        "submit_entry {} context changed. previous:{} now:{}",
                        entry.proposal_short_id(),
                        pre_resolve_tip,
                        tip_hash
                    );

                    // destructuring assignments are not currently supported
                    status = check_rtx(tx_pool, &snapshot, &entry.rtx)?;

                    let tip_header = snapshot.tip_header();
                    let tx_env = status.with_env(tip_header);
                    time_relative_verify(snapshot, Arc::clone(&entry.rtx), tx_env)?;
                }

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

                tx_pool.remove_conflict(&entry.proposal_short_id());
                tx_pool
                    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
                    .map_or(Ok(()), Err)?;
```

**File:** tx-pool/src/process.rs (L836-851)
```rust
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

**File:** tx-pool/src/process.rs (L1109-1113)
```rust
    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
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
