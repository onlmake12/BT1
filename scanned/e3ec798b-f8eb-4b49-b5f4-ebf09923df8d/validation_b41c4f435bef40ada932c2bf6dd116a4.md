The code evidence is conclusive. Here is the full analysis:

---

### Title
Write Lock Held Across Unbounded Synchronous Script Execution During Reorg Re-verification — (`tx-pool/src/process.rs`)

### Summary

`update_tx_pool_for_reorg` acquires the `tx_pool` write lock and, while holding it, calls `readd_detached_tx`, which loops over every detached transaction and calls `verify_rtx` with `command_rx = None`. `verify_rtx` in that branch executes full script verification via `block_in_place`, a synchronous blocking call. The write lock is never released between transactions. Any concurrent operation requiring the pool lock — tx submission, block template generation, RPC — is blocked for the entire duration.

### Finding Description

**Lock acquisition:** [1](#0-0) 

```
let mut tx_pool = self.tx_pool.write().await;   // write lock acquired

_update_tx_pool_for_reorg(...);

// notice: readd_detached_tx don't update cache
self.readd_detached_tx(&mut tx_pool, retain, fetched_cache)
    .await;
// write lock released here, at end of block
```

**`readd_detached_tx` calls `verify_rtx` with `command_rx = None` for every detached tx, while `tx_pool` (the write guard) is still in scope:** [2](#0-1) 

**`verify_rtx` with `command_rx = None` takes the `block_in_place` branch — full synchronous script execution, no yield point, no cancellation:** [3](#0-2) 

```rust
} else {
    block_in_place(|| {
        ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
            .verify(max_tx_verify_cycles, false)   // full script run, up to max_tx_verify_cycles
            ...
    })
}
```

`block_in_place` tells Tokio to move other tasks off the current thread, but it does **not** release the tokio `RwLock` guard. The `tx_pool` variable holding the write guard remains live in the enclosing scope until line 851. Every other async task that calls `self.tx_pool.write().await` or `self.tx_pool.read().await` is blocked for the entire loop.

The cycle limit used is `self.tx_pool_config.max_tx_verify_cycles`: [4](#0-3) 

### Impact Explanation

While the write lock is held:
- `submit_entry` (all tx submissions) blocks
- `get_block_template` blocks (miners cannot produce new templates)
- All RPC calls that touch the pool block
- The reorg task loop itself is single-threaded (one `reorg_receiver` consumer), so a second reorg cannot be processed either [5](#0-4) 

The stall duration is `N_detached_txs × T_verify_per_tx`. With `max_tx_verify_cycles` at its default (70 000 000 cycles), a single max-cycle transaction takes on the order of hundreds of milliseconds on commodity hardware. A block containing dozens of such transactions produces a multi-second stall per detached block.

### Likelihood Explanation

The attacker does **not** need majority hashpower for a shallow reorg:

- A 1-block reorg is a normal network event; any miner can privately mine a block and release it to cause one.
- The attacker fills that block with transactions consuming `max_tx_verify_cycles` cycles each (valid scripts, valid PoW, valid chain).
- When the honest chain produces a competing block at the same height, the victim node processes the 1-block reorg and holds the write lock for the duration of re-verifying every transaction in the detached block.
- No special privilege, no leaked key, no majority hashpower required for the 1-block case.

For deeper reorgs the hashpower requirement grows, but the 1-block case alone is sufficient to demonstrate the invariant violation.

### Recommendation

1. **Release the lock before verification.** Collect the set of transactions to re-add, drop the write lock, verify each transaction independently (without holding any pool lock), then re-acquire the write lock only to insert the verified entries.
2. **Use `verify_with_pause` (the `command_rx` path).** The async, interruptible path already exists in `verify_rtx`; `readd_detached_tx` should pass a `command_rx` so verification yields to the Tokio scheduler and can be cancelled.
3. **Cap the number of transactions re-verified per reorg event** to bound worst-case stall time.

### Proof of Concept

```
1. Craft a transaction T with a lock script that consumes exactly max_tx_verify_cycles cycles.
2. Mine block B1 (privately) at height H containing K copies of T (different inputs).
3. Let the honest network also produce block B1' at height H.
4. Release B1 to a victim node that already has B1' as tip → 1-block reorg.
5. The victim node calls update_tx_pool_for_reorg with detached=[B1], attached=[B1'].
6. readd_detached_tx iterates over K transactions, calling verify_rtx (block_in_place) for each.
7. The tx_pool write lock is held for K × T_verify_per_tx seconds.
8. During this window, submit any RPC call (get_block_template, send_transaction) and observe it hangs.
```

The invariant — *the tx_pool write lock must never be held across unbounded script execution* — is concretely violated at: [1](#0-0) [6](#0-5)

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

**File:** tx-pool/src/util.rs (L116-131)
```rust
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

**File:** tx-pool/src/service.rs (L698-731)
```rust
        self.handle.spawn(async move {
            loop {
                tokio::select! {
                    Some(message) = reorg_receiver.recv() => {
                        let Notify {
                            arguments: (detached_blocks, attached_blocks, detached_proposal_id, snapshot),
                        } = message;
                        let snapshot_clone = Arc::clone(&snapshot);
                        let detached_blocks_clone = detached_blocks.clone();
                        service.update_block_assembler_before_tx_pool_reorg(
                            detached_blocks_clone,
                            snapshot_clone
                        ).await;

                        let snapshot_clone = Arc::clone(&snapshot);
                        service
                        .update_tx_pool_for_reorg(
                            detached_blocks,
                            attached_blocks,
                            detached_proposal_id,
                            snapshot_clone,
                        )
                        .await;

                        service.update_block_assembler_after_tx_pool_reorg().await;
                    },
                    _ = signal_receiver.cancelled() => {
                        info!("TxPool reorg process service received exit signal, exit now");
                        break
                    },
                    else => break,
                }
            }
        });
```
