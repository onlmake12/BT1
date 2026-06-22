The code evidence is conclusive. Here is the full analysis:

---

### Title
`readd_detached_tx` Holds `tx_pool` Write Lock During CKB-VM Script Execution — (`tx-pool/src/process.rs`)

### Summary

`TxPoolService::update_tx_pool_for_reorg` acquires the `tx_pool` write lock and holds it for the entire duration of `readd_detached_tx`, which calls `verify_rtx` (full CKB-VM execution via `block_in_place`) for every detached transaction. Any miner who triggers a reorg with uncached, high-cycle transactions causes all other `tx_pool` read and write lock acquisitions to block for the full re-verification window.

### Finding Description

In `tx-pool/src/process.rs`, `update_tx_pool_for_reorg` acquires the write lock at line 836 and does not release it until line 851, spanning the entire `readd_detached_tx` call:

```
let mut tx_pool = self.tx_pool.write().await;   // lock acquired
_update_tx_pool_for_reorg(&mut tx_pool, ...);
self.readd_detached_tx(&mut tx_pool, retain, fetched_cache).await;
// lock released here
``` [1](#0-0) 

Inside `readd_detached_tx`, for each detached transaction that is not in the pre-fetched cache, `verify_rtx` is called with `command_rx = None`: [2](#0-1) 

In `tx-pool/src/util.rs`, when `command_rx` is `None` and there is no cache hit, `verify_rtx` falls into the `block_in_place` branch, running full synchronous CKB-VM execution: [3](#0-2) 

`block_in_place` moves the blocking work off the async executor thread, but it does **not** release the `tx_pool` write lock guard — the guard is still owned by the calling async task. Any other task that calls `tx_pool.write().await` or `tx_pool.read().await` will suspend until the lock is released.

This is the **opposite** of the correct pattern used in `_process_tx`, where `verify_rtx` is called **before** acquiring the write lock, and only the fast `submit_entry` call holds it: [4](#0-3) 

### Impact Explanation

While the write lock is held during re-verification:

- **`submit_entry`** (new tx admission) calls `with_tx_pool_write_lock` → blocked. [5](#0-4) 

- **`pre_check`** for incoming transactions calls `with_tx_pool_read_lock` → blocked (tokio `RwLock` write lock excludes all readers). [6](#0-5) 

- **Block assembler updates** (`update_full`, `update_proposals`, `update_transactions`) all call `tx_pool.read().await` → blocked. [7](#0-6) 

- **`get_block_template`** returns the cached template without acquiring the lock → **not** blocked, so miners can still get stale templates. [8](#0-7) 

The stall duration is bounded by `N_detached_txs × max_tx_verify_cycles / CPU_speed`. For a single block filled with max-cycle transactions (consensus `max_block_cycles` ≈ 3.5 × 10⁹ cycles), this is several seconds of lock hold time per reorg event.

### Likelihood Explanation

The attacker must be a miner with enough hashpower to produce at least one competing block. They mine a private block containing many high-cycle transactions that were **never submitted to the network's tx pool** (bypassing the `txs_verify_cache`). When submitted, the reorg triggers `readd_detached_tx` with an empty cache for all detached txs, maximizing lock hold time. A 1-block reorg requires only that the attacker mines a block at the same height as the current tip — achievable by any miner with any nonzero hashpower given enough attempts.

### Recommendation

Move `verify_rtx` calls **outside** the `tx_pool` write lock, mirroring the pattern in `_process_tx`. Collect all `(rtx, status, fee, tx_size, verified)` tuples first (without holding the lock), then acquire the write lock only for the fast `_submit_entry` calls. Alternatively, pass a `command_rx` to `verify_rtx` in `readd_detached_tx` so verification can be interrupted, and release/reacquire the lock between transactions.

### Proof of Concept

1. Attacker mines a private chain of 1 block containing ~N transactions, each consuming `max_tx_verify_cycles` cycles, none of which were ever broadcast to the network (so the victim's `txs_verify_cache` is cold for all of them).
2. Attacker submits the block to the victim node when it is at the same height, triggering a 1-block reorg.
3. Victim node calls `update_tx_pool_for_reorg` → acquires `tx_pool.write()` → calls `readd_detached_tx` → calls `verify_rtx(..., None)` for each of the N transactions via `block_in_place`.
4. During this window, measure time-to-acquire for a concurrent `tx_pool.write()` or `tx_pool.read()` call (e.g., from a `submit_remote_tx` or `update_full` path).
5. Assert that the lock hold time exceeds an acceptable threshold (e.g., > 1 second for a block with many max-cycle txs). [1](#0-0) [9](#0-8)

### Citations

**File:** tx-pool/src/process.rs (L66-74)
```rust
    pub(crate) async fn get_block_template(&self) -> Result<BlockTemplate, AnyError> {
        if let Some(ref block_assembler) = self.block_assembler {
            Ok(block_assembler.get_current().await)
        } else {
            Err(InternalErrorKind::Config
                .other("BlockAssembler disabled")
                .into())
        }
    }
```

**File:** tx-pool/src/process.rs (L96-103)
```rust
    pub(crate) async fn submit_entry(
        &self,
        pre_resolve_tip: Byte32,
        entry: TxEntry,
        mut status: TxStatus,
    ) -> (Result<(), Reject>, Arc<Snapshot>) {
        let (ret, snapshot) = self
            .with_tx_pool_write_lock(move |tx_pool, snapshot| {
```

**File:** tx-pool/src/process.rs (L247-256)
```rust
    pub(crate) async fn with_tx_pool_read_lock<U, F: FnMut(&TxPool, Arc<Snapshot>) -> U>(
        &self,
        mut f: F,
    ) -> (U, Arc<Snapshot>) {
        let tx_pool = self.tx_pool.read().await;
        let snapshot = tx_pool.cloned_snapshot();

        let ret = f(&tx_pool, Arc::clone(&snapshot));
        (ret, snapshot)
    }
```

**File:** tx-pool/src/process.rs (L724-753)
```rust
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

**File:** tx-pool/src/process.rs (L895-903)
```rust
                if let Ok(verified) = verify_rtx(
                    snapshot,
                    Arc::clone(&rtx),
                    tx_env,
                    &verify_cache,
                    max_cycles,
                    None,
                )
                .await
```

**File:** tx-pool/src/util.rs (L85-132)
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
}
```

**File:** tx-pool/src/block_assembler/mod.rs (L190-214)
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
        };
```
