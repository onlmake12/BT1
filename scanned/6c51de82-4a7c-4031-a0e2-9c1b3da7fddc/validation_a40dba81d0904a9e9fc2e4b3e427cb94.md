### Title
Tx-Pool Size Limit Bypassed After Reorg via `readd_detached_tx` - (File: `tx-pool/src/process.rs`)

---

### Summary

During a chain reorganization, `readd_detached_tx` re-inserts detached-block transactions into the tx-pool **after** `limit_size` has already been enforced, with no subsequent size check. This allows `total_tx_size` to exceed `max_tx_pool_size`, directly analogous to the reported pattern of bypassing a cap check by calling an internal accounting path that skips the limit guard.

---

### Finding Description

In `update_tx_pool_for_reorg` (`tx-pool/src/process.rs`, lines 802–858), the reorg handler acquires the pool write-lock and calls two functions sequentially:

```rust
{
    let mut tx_pool = self.tx_pool.write().await;
    _update_tx_pool_for_reorg(          // ← calls limit_size at its end
        &mut tx_pool, &attached,
        &detached_headers, detached_proposal_id,
        snapshot, &self.callbacks, mine_mode,
    );
    // notice: readd_detached_tx don't update cache
    self.readd_detached_tx(&mut tx_pool, retain, fetched_cache).await;
    // ← NO limit_size called here
}
```

`_update_tx_pool_for_reorg` ends with an explicit size enforcement:

```rust
// Remove transactions from the pool until its size <= size_limit.
let _ = tx_pool.limit_size(callbacks, None);
```

Immediately after, `readd_detached_tx` re-adds every transaction from detached blocks that is absent from the newly-attached chain (`retain = detached.difference(&attached)`). These transactions were previously committed (not in the pool), so they represent a net addition to `total_tx_size`. The insertion path is:

```
readd_detached_tx
  → _submit_entry
    → add_pending / add_gap / add_proposed   (tx-pool/src/pool.rs:131-149)
      → pool_map.add_entry                   (tx-pool/src/component/pool_map.rs:200-221)
        → updated_stat_for_add_tx            (pool_map.rs:711-729)
```

`updated_stat_for_add_tx` only guards against arithmetic overflow; it does **not** compare against `max_tx_pool_size`:

```rust
fn updated_stat_for_add_tx(&self, tx_size: usize, cycles: Cycle)
    -> Result<(usize, Cycle), Reject>
{
    let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
        Reject::Full(format!("tx-pool total_tx_size {} overflows ...", ...))
    })?;
    let total_tx_cycles = self.total_tx_cycles.checked_add(cycles).ok_or_else(|| {
        Reject::Full(format!("tx-pool total_tx_cycles {} overflows ...", ...))
    })?;
    Ok((total_tx_size, total_tx_cycles))
}
```

The `max_tx_pool_size` guard lives exclusively in `limit_size` (`pool.rs:292-329`), which is never called after `readd_detached_tx`. The pool therefore exits the reorg handler with `total_tx_size > max_tx_pool_size`.

---

### Impact Explanation

`max_tx_pool_size` is the node's primary memory-budget control for the tx-pool. After a reorg, the pool can exceed this limit by up to the aggregate serialized size of all transactions in the detached blocks (bounded by `max_block_bytes` per detached block, roughly 597 KB per block in CKB). The excess persists until the next ordinary transaction submission triggers `limit_size`. During that window the node consumes more memory than its operator configured, and the eviction policy (which protects against DoS) is not applied to the re-added transactions.

---

### Likelihood Explanation

Low. A reorg is required. A single-block natural reorg (which occurs occasionally on any live network) is sufficient to trigger the condition if the detached block contains transactions. A miner with even a small fraction of hashpower can deliberately produce competing blocks to trigger repeated reorgs, but this is costly and the excess is bounded per reorg event.

---

### Recommendation

Call `limit_size` after `readd_detached_tx` completes, inside the same lock scope:

```rust
{
    let mut tx_pool = self.tx_pool.write().await;
    _update_tx_pool_for_reorg(...);
    self.readd_detached_tx(&mut tx_pool, retain, fetched_cache).await;
    let _ = tx_pool.limit_size(&self.callbacks, None); // add this
}
```

Alternatively, integrate the `max_tx_pool_size` check into `updated_stat_for_add_tx` so that every insertion path is guarded uniformly.

---

### Proof of Concept

1. Node A has a tx-pool at or near `max_tx_pool_size`.
2. A competing miner produces a valid block B' containing N large transactions (total size S bytes, S ≤ `max_block_bytes`). These transactions are not in node A's pool.
3. Node A's chain tip is replaced by B' (1-block reorg). `update_tx_pool_for_reorg` is called.
4. `_update_tx_pool_for_reorg` runs; `limit_size` brings `total_tx_size` to ≤ `max_tx_pool_size`.
5. `readd_detached_tx` re-inserts the N transactions from the now-detached block into the pool via `add_entry` → `updated_stat_for_add_tx`, adding S bytes to `total_tx_size` with no cap check.
6. The pool exits the reorg handler with `total_tx_size = max_tx_pool_size + S`, exceeding the configured limit.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

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

**File:** tx-pool/src/process.rs (L1109-1114)
```rust
    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
}
```

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
