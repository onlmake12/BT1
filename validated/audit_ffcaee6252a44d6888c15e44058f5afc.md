### Title
Tx-Pool Write Lock Held Across CKB-VM Script Execution in `readd_detached_tx` During Reorg — (`tx-pool/src/process.rs`)

---

### Summary

During chain reorg processing, `readd_detached_tx` is called while the `tx_pool` write lock is held. Inside this function, `verify_rtx` is awaited with `command_rx = None`, which routes to `block_in_place` for synchronous CKB-VM script execution. The write lock is held for the entire duration of script execution per detached transaction, blocking all concurrent tx-pool operations.

---

### Finding Description

In `update_tx_pool_for_reorg`, the write lock on `self.tx_pool` is acquired and held across the entire call to `readd_detached_tx`:

```rust
{
    let mut tx_pool = self.tx_pool.write().await;   // write lock acquired
    _update_tx_pool_for_reorg(&mut tx_pool, ...);
    self.readd_detached_tx(&mut tx_pool, retain, fetched_cache)
        .await;                                      // lock held across this await
}
``` [1](#0-0) 

Inside `readd_detached_tx`, for each detached transaction not found in the verification cache, `verify_rtx` is called with `command_rx = None`:

```rust
if let Ok(verified) = verify_rtx(
    snapshot,
    Arc::clone(&rtx),
    tx_env,
    &verify_cache,
    max_cycles,
    None,          // ← forces block_in_place path
)
.await
``` [2](#0-1) 

In `verify_rtx`, the `command_rx = None` branch uses `block_in_place` to run `ContextualTransactionVerifier` synchronously:

```rust
} else {
    block_in_place(|| {
        ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
            .verify(max_tx_verify_cycles, false)
            ...
    })
}
``` [3](#0-2) 

`block_in_place` runs the closure on the current thread (moved to a blocking thread pool), but the `tokio::sync::RwLock` write guard is still held by the current async task for the entire duration. Any other async task attempting to acquire the write lock — including `submit_entry`, `save_pool`, `clear_pool`, or another reorg — is blocked until all detached transactions finish re-verification.

The verification cache (`fetched_cache`) is populated before the write lock is acquired:

```rust
let fetched_cache = self.fetch_txs_verify_cache(retain.iter()).await;
``` [4](#0-3) 

The cache is only populated when a transaction passes through `_process_tx` on this node. Transactions that were mined without passing through this node's mempool (e.g., received via compact block relay) will have no cache entry, forcing the full `block_in_place` path for every such transaction in the detached set.

The cache update itself is a spawned task that runs asynchronously after `_process_tx` completes:

```rust
tokio::spawn(async move {
    let mut guard = txs_verify_cache.write().await;
    guard.put(wtx_hash, verified);
});
``` [5](#0-4) 

This means even transactions that were previously verified may have their cache entry evicted (LRU) or not yet written before a reorg occurs.

---

### Impact Explanation

While the write lock is held during `verify_rtx` for each detached transaction:

- `submit_entry` (new transaction admission) is blocked — the tx-pool cannot accept new transactions
- `with_tx_pool_write_lock` in `submit_entry` blocks — concurrent `_process_tx` workers stall
- `save_pool` (graceful shutdown) is blocked
- Block template generation via `update_full` is blocked — miners cannot produce new block templates

For N detached transactions each running up to `max_tx_verify_cycles` cycles, the total lock hold time is proportional to N × (max script execution time). With `max_tx_verify_cycles` set to a large value (e.g., 70,000,000), each transaction can hold the lock for several seconds. [6](#0-5) 

---

### Likelihood Explanation

An unprivileged attacker can trigger this without majority hashpower:

1. Craft a transaction with a lock/type script that consumes close to `max_tx_verify_cycles` cycles.
2. Submit it directly to a miner's node (bypassing the victim node's mempool), so the victim node's `txs_verify_cache` has no entry for it.
3. The miner includes it in Block N.
4. The victim node receives Block N and stores it.
5. The attacker (or any competing miner) mines a competing Block N′ at the same height, causing a 1-block reorg on the victim node.
6. During `update_tx_pool_for_reorg`, the victim node calls `readd_detached_tx` with the complex-script transaction in `retain`.
7. Cache miss → `block_in_place` runs full CKB-VM execution while holding the write lock.

A 1-block reorg requires no majority hashpower — it occurs naturally when two miners find blocks simultaneously, and an attacker with any mining capacity can deliberately produce a competing block. The attacker can repeat this to sustain the DoS.

---

### Recommendation

Move script verification outside the write lock scope. Resolve and verify all detached transactions before acquiring the write lock, then acquire the lock only to call `_submit_entry`:

```rust
// Phase 1: resolve and verify outside the lock
let mut verified_entries = Vec::new();
for tx in txs {
    if let Ok((rtx, status)) = resolve_tx_no_lock(...) {
        if let Ok(verified) = verify_rtx(..., None).await {  // no lock held
            verified_entries.push((rtx, status, verified, fee, tx_size));
        }
    }
}

// Phase 2: acquire write lock only for pool mutation
{
    let mut tx_pool = self.tx_pool.write().await;
    for (rtx, status, verified, fee, tx_size) in verified_entries {
        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
        let _ = _submit_entry(&mut tx_pool, status, entry, &self.callbacks);
    }
}
```

This mirrors the pattern already used in `_process_tx`, where `pre_check` (read lock), `verify_rtx` (no lock), and `submit_entry` (write lock) are separated. [7](#0-6) 

---

### Proof of Concept

```
1. Attacker crafts TX_heavy: lock script loops for max_tx_verify_cycles cycles.
2. Attacker submits TX_heavy directly to Miner_M (not through victim node's RPC).
   → victim node's txs_verify_cache has no entry for TX_heavy.
3. Miner_M mines Block_N containing TX_heavy.
4. Victim node receives Block_N via P2P, stores it, updates snapshot.
5. Attacker mines Block_N' at the same height (any hashpower).
6. Victim node receives Block_N', triggers reorg:
   chain/src/verify.rs → tx_pool_controller.update_tx_pool_for_reorg(...)
7. update_tx_pool_for_reorg:
   - retain = [TX_heavy]  (detached from Block_N, not in Block_N')
   - fetched_cache = {}   (cache miss)
   - tx_pool.write().await acquired
   - readd_detached_tx(&mut tx_pool, [TX_heavy], {}) called
   - verify_rtx(TX_heavy, None) → block_in_place → CKB-VM runs max_cycles
   - write lock held for entire VM execution (several seconds)
8. During step 7, all calls to tx_pool.write().await block:
   - submit_entry for any new transaction → stalled
   - block assembler update → stalled
   - save_pool on shutdown → stalled
9. Attacker repeats step 5 to sustain the DoS.
``` [8](#0-7) [6](#0-5) [9](#0-8)

### Citations

**File:** tx-pool/src/process.rs (L705-753)
```rust
    pub(crate) async fn _process_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Option<Cycle>,
        command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
    ) -> Option<(Result<Completed, Reject>, Arc<Snapshot>)> {
        let wtx_hash = tx.witness_hash();
        let instant = Instant::now();
        let is_sync_process = command_rx.is_none();

        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);

        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

        let verified_ret = verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            command_rx,
        )
        .await;

        let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

        if let Some(declared) = declared_cycles
            && declared != verified.cycles
        {
            info!(
                "process_tx declared cycles not match verified cycles, declared: {}, verified: {}, tx_hash: {}",
                declared,
                verified.cycles,
                tx.hash()
            );
            return Some((
                Err(Reject::DeclaredWrongCycles(declared, verified.cycles)),
                snapshot,
            ));
        }

        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
```

**File:** tx-pool/src/process.rs (L761-764)
```rust
            tokio::spawn(async move {
                let mut guard = txs_verify_cache.write().await;
                guard.put(wtx_hash, verified);
            });
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

**File:** tx-pool/src/util.rs (L85-131)
```rust
pub(crate) async fn verify_rtx(
    snapshot: Arc<Snapshot>,
    rtx: Arc<ResolvedTransaction>,
    tx_env: Arc<TxVerifyEnv>,
    cache_entry: &Option<CacheEntry>,
    max_tx_verify_cycles: Cycle,
    command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
) -> Result<Completed, Reject> {
    let consensus = snapshot.cloned_consensus();
    let data_loader = snapshot.as_data_loader();

    if let Some(completed) = cache_entry {
        TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
            .verify()
            .map(|_| *completed)
            .map_err(Reject::Verification)
    } else if let Some(command_rx) = command_rx {
        ContextualTransactionVerifier::new(
            Arc::clone(&rtx),
            consensus,
            data_loader,
            Arc::clone(&tx_env),
        )
        .verify_with_pause(max_tx_verify_cycles, command_rx)
        .await
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
    } else {
        block_in_place(|| {
            ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
                .verify(max_tx_verify_cycles, false)
                .and_then(|result| {
                    DaoScriptSizeVerifier::new(
                        rtx,
                        snapshot.cloned_consensus(),
                        snapshot.as_data_loader(),
                    )
                    .verify()?;
                    Ok(result)
                })
                .map_err(Reject::Verification)
        })
    }
```
