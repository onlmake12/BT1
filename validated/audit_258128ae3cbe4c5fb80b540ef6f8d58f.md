Audit Report

## Title
TOCTOU Race in `_process_tx` Allows Spurious Tx-Pool Eviction via Concurrent Submission Paths — (`tx-pool/src/process.rs`)

## Summary

The service loop spawns each incoming message as an independent tokio task without awaiting, allowing a concurrent RPC `SubmitLocalTx` and P2P `SubmitRemoteTx` for the same transaction to both reach `_process_tx`. Both pass `pre_check`'s `check_txid_collision` under a read lock before either inserts the transaction. `submit_entry`'s write-lock closure does not re-check for txid collision, and `limit_size` is called unconditionally regardless of whether `_submit_entry` actually inserted the transaction (`succ=false`). When the pool is near capacity, this causes one spurious eviction of a legitimate third-party transaction and double-relays the transaction to peers.

## Finding Description

**Concurrent task spawning.** The service loop at `service.rs:619-622` calls `handle_clone.spawn(process(service_clone, message))` without awaiting, making every message an independent tokio task. Two messages for the same transaction (one RPC, one P2P) therefore run concurrently. [1](#0-0) 

**Path A (RPC):** `process_tx` (line 401) checks `verify_queue_contains` at line 409, then calls `_process_tx` directly — it does **not** enqueue to the verify queue. [2](#0-1) 

**Path B (P2P):** `submit_remote_tx` (line 371) → `resumeble_process_tx` (line 335) → checks `verify_queue_contains` at line 349 → `enqueue_verify_queue` → worker → `_process_tx`. Because Task A never enqueued, Task B's `verify_queue_contains` check also passes. [3](#0-2) 

**`pre_check` uses a read lock.** Both tasks call `pre_check`, which acquires a read lock and calls `check_txid_collision`. Since neither task has inserted the tx yet, both pass. [4](#0-3) [5](#0-4) 

**`submit_entry` does not re-check for collision.** The write-lock closure in `submit_entry` re-checks RBF and tip-hash (with an explicit comment "must be invoked in `write` lock to avoid concurrent issues"), but never calls `check_txid_collision`. `_submit_entry` calls `pool_map.add_entry`, which silently returns `(false, empty_set)` for a duplicate without error. [6](#0-5) [7](#0-6) 

**`_submit_entry` returns `Ok(evicts)` regardless of `succ`.** The `succ` flag only gates callbacks; the function always returns `Ok(evicts)`. [8](#0-7) 

**`limit_size` is called unconditionally.** After `_submit_entry` returns, `submit_entry` calls `limit_size` with no guard on whether the tx was actually inserted. When the second `submit_entry` runs (`succ=false`, tx already present), `limit_size` still executes and, if the pool is at capacity, evicts a legitimate transaction. [9](#0-8) 

**Double relay.** Both `_process_tx` invocations return `Some((Ok(verified), snapshot))` at line 776. Both callers invoke `after_process`, which calls `send_result_to_relayer(TxVerificationResult::Ok {...})` and `process_orphan_tx` — each fires twice. [10](#0-9) 

## Impact Explanation

When the tx pool is at or near its size limit, the second spurious `limit_size` call evicts one legitimate third-party transaction that would not otherwise have been evicted. The double `send_result_to_relayer` and `process_orphan_tx` calls waste peer bandwidth and processing. Pool state integrity is preserved (the duplicate is silently ignored by `add_entry`), so the impact is bounded to one extra eviction and one extra relay per race instance. This fits **Low (501–2000 points): Any other important performance improvements for CKB**.

## Likelihood Explanation

The race window spans the entire `verify_rtx` execution — potentially seconds for high-cycle scripts. The service loop spawns each message as a separate task, making concurrent execution the normal operating mode. Triggering the race requires: (1) RPC access (typically localhost-bound, limiting external exploitation) and (2) a P2P peer connection (default open). The attacker must also fill the pool near capacity to cause an actual eviction. These preconditions are achievable but not trivially so for an external attacker; a co-located or local actor can trigger this reliably and repeatedly.

## Recommendation

Re-check for txid collision inside `submit_entry`'s write-lock closure, before calling `_submit_entry`, mirroring the existing pattern for `check_rbf`:

```rust
// Inside submit_entry's write-lock closure, before _submit_entry:
check_txid_collision(tx_pool, entry.transaction())?;
```

Additionally, gate `limit_size` behind the `succ` result. Propagate `succ` out of `_submit_entry` (or check pool membership after the call) and only invoke `limit_size` when the transaction was actually inserted:

```rust
let (succ, evicts) = /* propagated from _submit_entry */;
if succ {
    tx_pool.limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
        .map_or(Ok(()), Err)?;
}
```

## Proof of Concept

1. Start a CKB node with default config. Connect as both an RPC client and a P2P peer.
2. Fill the tx pool to within one slot of its size limit using low-fee-rate transactions.
3. Construct a valid transaction `T` with a script that runs near `max_tx_verify_cycles`.
4. Simultaneously send two service messages for `T`:
   - RPC `send_transaction` → `SubmitLocalTx` message → spawned as Task A.
   - P2P `RelayTransaction` → `SubmitRemoteTx` message → spawned as Task B.
5. Task A: `process_tx` → `verify_queue_contains` (empty) → `_process_tx` → `pre_check` (passes) → `verify_rtx` (long await).
6. Task B: `submit_remote_tx` → `resumeble_process_tx` → `verify_queue_contains` (empty, Task A did not enqueue) → `enqueue_verify_queue` → worker → `_process_tx` → `pre_check` (passes, tx not in pool) → `verify_rtx` (long await).
7. Both complete `verify_rtx`. Both call `submit_entry` under the write lock (serialized).
8. First `submit_entry`: `add_entry` returns `(true, ...)` → tx inserted → `limit_size` evicts tx A (pool was full).
9. Second `submit_entry`: `add_entry` returns `(false, empty)` → tx NOT inserted → `limit_size` still called → evicts tx B.
10. Observe two legitimate transactions evicted instead of one, and `send_result_to_relayer` called twice.

### Citations

**File:** tx-pool/src/service.rs (L619-622)
```rust
                    Some(message) = receiver.recv() => {
                        let service_clone = process_service.clone();
                        handle_clone.spawn(process(service_clone, message));
                    },
```

**File:** tx-pool/src/process.rs (L102-137)
```rust
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
```

**File:** tx-pool/src/process.rs (L149-152)
```rust
                tx_pool.remove_conflict(&entry.proposal_short_id());
                tx_pool
                    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
                    .map_or(Ok(()), Err)?;
```

**File:** tx-pool/src/process.rs (L269-283)
```rust
    pub(crate) async fn pre_check(
        &self,
        tx: &TransactionView,
    ) -> (Result<PreCheckedTx, Reject>, Arc<Snapshot>) {
        // Acquire read lock for cheap check
        let tx_size = tx.data().serialized_size_in_block();

        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

```

**File:** tx-pool/src/process.rs (L335-353)
```rust
    pub(crate) async fn resumeble_process_tx(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
        self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
    }
```

**File:** tx-pool/src/process.rs (L401-415)
```rust
    pub(crate) async fn process_tx(
        &self,
        tx: TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<Completed, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.verify_queue_contains(&tx).await || self.orphan_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }

        if let Some((ret, snapshot)) = self
            ._process_tx(tx.clone(), remote.map(|r| r.0), None)
            .await
```

**File:** tx-pool/src/process.rs (L489-501)
```rust
        match remote {
            Some((declared_cycle, peer)) => match ret {
                Ok(_) => {
                    debug!(
                        "after_process remote send_result_to_relayer {} {}",
                        tx_hash, peer
                    );
                    self.send_result_to_relayer(TxVerificationResult::Ok {
                        original_peer: Some(peer),
                        tx_hash,
                    });
                    self.process_orphan_tx(&tx).await;
                }
```

**File:** tx-pool/src/process.rs (L1024-1036)
```rust
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
```

**File:** tx-pool/src/util.rs (L20-26)
```rust
pub(crate) fn check_txid_collision(tx_pool: &TxPool, tx: &TransactionView) -> Result<(), Reject> {
    let short_id = tx.proposal_short_id();
    if tx_pool.contains_proposal_id(&short_id) {
        return Err(Reject::Duplicated(tx.hash()));
    }
    Ok(())
}
```

**File:** tx-pool/src/component/pool_map.rs (L207-209)
```rust
        if self.entries.get_by_id(&tx_short_id).is_some() {
            return Ok((false, evicts));
        }
```
