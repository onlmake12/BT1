Audit Report

## Title
TOCTOU Race in `_process_tx` Allows Concurrent Duplicate Script Verification — (File: `tx-pool/src/process.rs`)

## Summary
In `TxPoolService::_process_tx`, the duplicate-membership check (`check_txid_collision`) is performed under a short-lived read lock that is released before the expensive `verify_rtx` script-execution step. Because the service loop spawns an independent Tokio task per message, two concurrent `SubmitLocalTx` submissions of the same transaction can both pass all duplicate guards and both execute full RISC-V script verification before either one commits the transaction to the pool. The second `submit_entry` is rejected as a duplicate, but both verifications have already consumed CPU. With a channel capacity of 512, an attacker with local RPC access can trigger up to 512 concurrent full-cycle verifications of a single transaction, saturating CPU and rendering the node unresponsive.

## Finding Description

**Service loop spawns one task per message** — `service.rs:619-621`: [1](#0-0) 

This means concurrent `SubmitLocalTx` messages run fully in parallel.

**`_process_tx` execution order** — `process.rs:715-753`:

1. `pre_check` at line 715 acquires a read lock via `with_tx_pool_read_lock`, calls `check_txid_collision` inside the closure, then **drops the lock when the closure returns** (line 313-314). [2](#0-1) 

2. `verify_rtx` at lines 724-732 runs with **no lock held**. For the local path (`command_rx = None`), this calls `block_in_place`, blocking a Tokio worker thread for the full duration of RISC-V script execution. [3](#0-2) 

3. `submit_entry` at line 753 acquires a write lock and performs the first write that prevents a duplicate. [4](#0-3) 

**Earlier guards in `process_tx` also release their locks before `_process_tx` is entered** — `process.rs:409-414`: [5](#0-4) 

`verify_queue_contains` and `orphan_contains` each acquire and immediately release their own read locks. Neither check covers the window between lock release and `submit_entry` completion.

**Race window**: From the moment `pre_check` releases its read lock to the moment `submit_entry` acquires its write lock, the transaction is invisible to all duplicate guards. This window spans the entire duration of `verify_rtx`. Two (or 512) concurrent tasks all observe an empty pool, all proceed to `verify_rtx`, and all race to `submit_entry`. Only the first insertion succeeds; all others are wasted.

**Contrast with the remote path** — `resumeble_process_tx` → `enqueue_verify_queue` acquires a write lock on `verify_queue` before returning, preventing duplicate enqueuing: [6](#0-5) 

The local `process_tx` path has no equivalent guard.

## Impact Explanation

`verify_rtx` for the local path uses `block_in_place`, blocking a Tokio worker thread per concurrent verification. With `DEFAULT_CHANNEL_SIZE = 512`: [7](#0-6) 

An attacker can enqueue 512 concurrent verifications of a single max-cycle transaction (~3.5 billion RISC-V cycles on mainnet). Each verification blocks a worker thread for hundreds of milliseconds. This exhausts the Tokio thread pool, preventing the node from processing block relay, sync, and mining template generation — matching the **High: Vulnerabilities which could easily crash a CKB node** impact class.

## Likelihood Explanation

The `send_transaction` RPC endpoint is the standard interface for wallets and SDKs. Any caller with local RPC access (default: `127.0.0.1:8114`) can issue 512 concurrent HTTP requests with no special privileges, keys, or network position. The race window is wide — hundreds of milliseconds for a max-cycle transaction — making concurrent exploitation straightforward with a simple shell loop. No victim mistakes or external context are required.

## Recommendation

Apply the check-effects-interactions pattern before releasing the read lock:

1. **Add an "in-verification" set** (e.g., `Arc<RwLock<HashSet<Byte32>>>`) to `TxPoolService`.
2. **Before releasing the lock in `pre_check`** (or immediately after, under a write lock), insert the tx hash into this set. Return `Reject::Duplicated` if it is already present.
3. **Remove the entry** from the set on completion (success or failure) of `verify_rtx` + `submit_entry`.
4. Extend `check_txid_collision` or add a new guard in `process_tx` to check this set atomically before entering `_process_tx`.

This mirrors the protection already present in `enqueue_verify_queue`, which holds a write lock on `verify_queue` before returning, preventing duplicate enqueuing on the remote path. [8](#0-7) 

## Proof of Concept

```bash
# Submit a max-cycle transaction 512 times concurrently via local RPC
for i in $(seq 1 512); do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"send_transaction","params":[<MAX_CYCLE_TX_HEX>,"passthrough"],"id":'"$i"'}' &
done
wait
```

All 512 tasks pass `pre_check` (pool is empty, no lock held during `verify_rtx`), all 512 invoke `verify_rtx` concurrently via `block_in_place`, exhausting Tokio worker threads. Only one `submit_entry` succeeds; 511 verifications are wasted. The node becomes unresponsive to block relay and sync for the duration of script execution. Observable via node logs showing 511 `Duplicated` rejections after all verifications complete, and via block relay latency spikes during the attack window.

### Citations

**File:** tx-pool/src/service.rs (L53-53)
```rust
pub(crate) const DEFAULT_CHANNEL_SIZE: usize = 512;
```

**File:** tx-pool/src/service.rs (L619-621)
```rust
                    Some(message) = receiver.recv() => {
                        let service_clone = process_service.clone();
                        handle_clone.spawn(process(service_clone, message));
```

**File:** tx-pool/src/process.rs (L276-314)
```rust
        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

                // Try normal path first, if double-spending check success we don't need RBF check
                // this make sure RBF won't introduce extra performance cost for hot path
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
                        if conflicts.is_none() {
                            // this mean one input's outpoint is dead, but there is no direct conflicted tx in tx_pool
                            // we should reject it directly and don't need to put it into conflicts pool
                            error!(
                                "{} is resolved as Dead, but there is no conflicted tx",
                                rtx.transaction.proposal_short_id()
                            );
                            return Err(Reject::Resolve(OutPointError::Dead(out)));
                        }
                        // we also return Ok here, so that the entry will be continue to be verified before submit
                        // we only want to put it into conflicts pool after the verification stage passed
                        // then we will double-check conflicts txs in `submit_entry`

                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(err) => Err(err),
                }
            })
            .await;
```

**File:** tx-pool/src/process.rs (L409-415)
```rust
        if self.verify_queue_contains(&tx).await || self.orphan_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }

        if let Some((ret, snapshot)) = self
            ._process_tx(tx.clone(), remote.map(|r| r.0), None)
            .await
```

**File:** tx-pool/src/process.rs (L753-754)
```rust
        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
        try_or_return_with_snapshot!(ret, submit_snapshot);
```

**File:** tx-pool/src/process.rs (L860-868)
```rust
    async fn enqueue_verify_queue(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        let mut queue = self.verify_queue.write().await;
        queue.add_tx(tx, is_proposal_tx, remote)
    }
```

**File:** tx-pool/src/util.rs (L116-130)
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
```
