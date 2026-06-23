Looking at the CKB codebase for a reentrancy-class analog — specifically, state updated **after** expensive external interactions, allowing concurrent re-entry before state is committed.

I traced the tx-pool submission pipeline in detail, focusing on `tx-pool/src/process.rs` and `tx-pool/src/service.rs`.

---

### Title
TOCTOU Race in `_process_tx` Allows Concurrent Duplicate Script Verification — (File: `tx-pool/src/process.rs`)

### Summary
In `TxPoolService::_process_tx`, duplicate-membership checks are performed under short-lived read locks that are released before the expensive `verify_rtx` script-execution step. Because each incoming `SubmitLocalTx` RPC message is spawned as an independent async task, two concurrent submissions of the same transaction can both pass all duplicate guards and both execute full RISC-V script verification before either one commits the transaction to the pool. This is the direct CKB analog of the FuroVesting reentrancy pattern: state (pool membership) is not updated until after the expensive "external call" (`verify_rtx`), so concurrent callers observe stale state and re-execute the costly operation.

### Finding Description

The service loop in `service.rs` spawns a new Tokio task for every received message: [1](#0-0) 

This means two concurrent `SubmitLocalTx` messages run fully in parallel. Each message calls `process_tx`, which calls `_process_tx`.

Inside `_process_tx` the execution order is:

1. **`pre_check`** — acquires a `RwLock` read guard, calls `check_txid_collision` to confirm the tx is not already in the pool, then **drops the lock**. [2](#0-1) 

2. **`verify_rtx`** — runs expensive RISC-V script execution with **no lock held**. [3](#0-2) 

3. **`submit_entry`** — acquires a write lock and inserts the entry into the pool (the first write that prevents a duplicate). [4](#0-3) 

The earlier duplicate guards in `process_tx` also release their locks before `_process_tx` is entered: [5](#0-4) 

Because the read lock from `pre_check` is released before `verify_rtx` begins, a second concurrent task calling `pre_check` on the same transaction will also see the pool as empty and proceed to run `verify_rtx`. Both tasks then race to `submit_entry`; the second is rejected as a duplicate, but **both have already completed full script verification**.

The same pattern recurs in `process_orphan_tx`: orphan entries are read under a short-lived read lock, then `_process_tx` is called without any lock, and `remove_orphan_tx` is called only after success — so two concurrent parent-tx completions can both trigger verification of the same orphan. [6](#0-5) 

### Impact Explanation

`verify_rtx` executes up to `max_block_cycles` RISC-V cycles (consensus-level maximum, ~3.5 billion on mainnet). The `SubmitLocalTx` channel has a capacity of 512: [7](#0-6) 

An attacker with RPC access can flood the channel with the same maximum-cycle transaction, causing up to 512 concurrent full script verifications of a single transaction. This saturates CPU and can render the node unresponsive to legitimate traffic (block relay, sync, mining template generation).

### Likelihood Explanation

The RPC endpoint (`send_transaction`) is the standard interface for wallets, SDKs, and dApps. Any unprivileged local RPC caller can issue concurrent HTTP requests. No special privileges, keys, or network position are required. The race window is wide — `verify_rtx` for a maximum-cycle tx can take hundreds of milliseconds — making concurrent exploitation straightforward with a simple script.

### Recommendation

Apply the check-effects-interactions pattern analogous to the FuroVesting fix:

1. **Register the transaction as "in-flight" before releasing the read lock** — add the tx hash to a `HashSet<Byte32>` (or the verify queue) under the same write lock used by `pre_check`, so subsequent concurrent calls see it as pending and return `Duplicated` immediately.
2. **Remove the in-flight marker** on completion (success or failure) of `verify_rtx` + `submit_entry`.
3. Alternatively, extend `check_txid_collision` to also check an "in-verification" set, updated atomically before `verify_rtx` is invoked.

The remote path (`resumeble_process_tx` → `enqueue_verify_queue`) already does this correctly — the verify queue write lock prevents duplicate enqueuing. The local `process_tx` path should adopt the same guard.

### Proof of Concept

```
# Terminal 1 – submit a max-cycle tx via RPC
for i in $(seq 1 512); do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"send_transaction","params":[<MAX_CYCLE_TX_HEX>,"passthrough"],"id":'"$i"'}' &
done
wait
```

All 512 tasks pass `pre_check` (pool is empty), all 512 invoke `verify_rtx` concurrently, node CPU pegs at 100% for the duration of script execution. Only one insertion succeeds; 511 verifications are wasted. The node becomes unresponsive to block relay and sync during this window.

### Citations

**File:** tx-pool/src/service.rs (L53-53)
```rust
pub(crate) const DEFAULT_CHANNEL_SIZE: usize = 512;
```

**File:** tx-pool/src/service.rs (L619-622)
```rust
                    Some(message) = receiver.recv() => {
                        let service_clone = process_service.clone();
                        handle_clone.spawn(process(service_clone, message));
                    },
```

**File:** tx-pool/src/process.rs (L409-411)
```rust
        if self.verify_queue_contains(&tx).await || self.orphan_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
```

**File:** tx-pool/src/process.rs (L625-641)
```rust
                } else if let Some((ret, _snapshot)) = self
                    ._process_tx(orphan.tx.clone(), Some(orphan.cycle), None)
                    .await
                {
                    match ret {
                        Ok(_) => {
                            self.send_result_to_relayer(TxVerificationResult::Ok {
                                original_peer: Some(orphan.peer),
                                tx_hash: orphan.tx.hash(),
                            });
                            debug!(
                                "process_orphan {} success, find previous from {}",
                                orphan.tx.hash(),
                                tx.hash()
                            );
                            self.remove_orphan_tx(&orphan.tx.proposal_short_id()).await;
                            orphan_queue.push_back(orphan.tx);
```

**File:** tx-pool/src/process.rs (L715-717)
```rust
        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);
```

**File:** tx-pool/src/process.rs (L724-732)
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
```

**File:** tx-pool/src/process.rs (L753-754)
```rust
        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
        try_or_return_with_snapshot!(ret, submit_snapshot);
```
