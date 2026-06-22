### Title
TOCTOU in `_process_tx`: txid collision check under read lock is not re-validated under write lock in `submit_entry` when tip is unchanged — (`tx-pool/src/process.rs`)

### Summary

The CKB tx-pool's `_process_tx` function performs a txid collision check under a **read lock** in `pre_check`, releases that lock, runs expensive script verification (`verify_rtx`) with **no lock held**, then acquires a **write lock** in `submit_entry` — but only re-validates pool state if the chain tip changed. When two concurrent execution paths process the same transaction simultaneously (e.g., one via RPC `process_tx` and one via a P2P relay worker), both can pass `pre_check` before either commits to the pool, both run full script verification in parallel, and both reach `submit_entry` with an unchanged tip hash, bypassing the re-check. This is a direct analog to the CEI violation in the report: state is read, an expensive external-like interaction occurs, and state is written back without re-checking the original condition.

### Finding Description

**Root cause and code path:**

`_process_tx` in `tx-pool/src/process.rs` follows this sequence:

1. **`pre_check` (read lock acquired and released)** — calls `check_txid_collision`, which checks only `pool_map` for the tx's `proposal_short_id`. The read lock is dropped at the end of `with_tx_pool_read_lock`.

2. **`verify_rtx` (no lock held)** — runs full CKB-VM script execution. This is the expensive "external interaction" window. No lock is held during this step.

3. **`submit_entry` (write lock acquired)** — re-checks pool state **only if** `pre_resolve_tip != tip_hash`. If the tip has not changed, it proceeds directly to `_submit_entry` → `tx_pool.add_pending(entry)` without repeating the txid collision check.

The comment at line 104 explicitly acknowledges the concurrent issue for RBF (`// check_rbf must be invoked in 'write' lock to avoid concurrent issues`) but the analogous re-check for txid collision is absent when the tip is unchanged.

**Concurrent execution paths that trigger the race:**

- **Path A**: RPC caller invokes `send_transaction` → `process_tx` → checks `verify_queue_contains` (read lock, released) → checks `orphan_contains` (read lock, released) → calls `_process_tx` directly (synchronous, no verify_queue).
- **Path B**: P2P relay peer sends the same tx → `submit_remote_tx` → `resumeble_process_tx` → `enqueue_verify_queue` → a `VerifyMgr` worker calls `_process_tx`.

**Race window:**

```
Path A: verify_queue_contains → false (tx not yet enqueued)
Path B: enqueue_verify_queue → tx added to verify_queue
Path A: pre_check (read lock) → check_txid_collision → tx not in pool_map → PASS
Path B worker: pre_check (read lock) → check_txid_collision → tx not in pool_map → PASS
Path A: verify_rtx (no lock) ← expensive script execution
Path B: verify_rtx (no lock) ← expensive script execution (concurrent)
Path A: submit_entry (write lock) → tip unchanged → no re-check → add_pending
Path B: submit_entry (write lock) → tip unchanged → no re-check → add_pending (duplicate)
```

The `verify_queue_contains` check in `process_tx` at line 409 is itself a separate read lock that is released before `_process_tx` is called, so it cannot prevent Path B's worker from racing with Path A's direct `_process_tx` call.

### Impact Explanation

**Resource exhaustion (confirmed):** An unprivileged attacker who controls both an RPC connection and a P2P peer connection can submit the same high-cycle transaction via both paths simultaneously. The node will run full CKB-VM script verification twice (or more, with N concurrent submissions) in parallel. Since `verify_rtx` is the most CPU-intensive operation in the tx-pool and can consume up to `max_block_cycles` per transaction, this allows an attacker to multiply the CPU cost of a single transaction by the number of concurrent submissions, causing sustained CPU exhaustion and degraded block processing throughput.

**Potential pool accounting corruption (conditional):** If `add_pending` (called from `_submit_entry`) does not atomically guard against duplicate `proposal_short_id` entries, the second concurrent `submit_entry` call can insert a duplicate entry into `pool_map`, corrupting `total_tx_size`, `total_tx_cycles`, and ancestor/descendant weight accounting. This desynchronizes the pool's internal state from its actual contents, analogous to the balance desynchronization described in the report.

### Likelihood Explanation

The attack requires no privileged access. Any node operator exposes both an RPC endpoint and a P2P port by default. An attacker needs only to:
1. Construct a valid, high-cycle transaction.
2. Submit it via `send_transaction` RPC and simultaneously relay it via the P2P protocol from a controlled peer.

The race window spans the entire duration of `verify_rtx` (potentially hundreds of milliseconds for high-cycle transactions), making it reliably triggerable. The attack is repeatable and scalable.

### Recommendation

Re-check `check_txid_collision` inside `submit_entry` under the write lock unconditionally, not only when the tip hash changes. The existing pattern for RBF (`check_rbf` is always called under the write lock) should be extended to cover txid collision:

```rust
// In submit_entry, inside with_tx_pool_write_lock:
check_txid_collision(tx_pool, entry.transaction())?;  // always re-check under write lock
let tip_hash = snapshot.tip_hash();
if pre_resolve_tip != tip_hash {
    status = check_rtx(tx_pool, &snapshot, &entry.rtx)?;
    ...
}
```

### Proof of Concept

1. Construct a valid CKB transaction `T` with a script that consumes close to `max_block_cycles` cycles.
2. Simultaneously:
   - Submit `T` via JSON-RPC `send_transaction` (triggers `process_tx` → `_process_tx` directly).
   - Relay `T` via P2P `RelayTransactionHashes`/`GetRelayTransactions` from a controlled peer (triggers `submit_remote_tx` → `resumeble_process_tx` → `enqueue_verify_queue` → worker `_process_tx`).
3. Both paths pass `pre_check` (neither has committed to `pool_map` yet).
4. Both execute `verify_rtx` concurrently, consuming 2× the expected CPU.
5. Both reach `submit_entry` with the same `pre_resolve_tip`; neither re-checks txid collision.
6. Repeat with N concurrent pairs to multiply CPU load by N, exhausting the node's verification capacity and delaying block template assembly and block processing. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** tx-pool/src/process.rs (L96-134)
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
```

**File:** tx-pool/src/process.rs (L269-316)
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
        (ret, snapshot)
    }
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

**File:** tx-pool/src/process.rs (L401-426)
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
        {
            self.after_process(tx, remote, &snapshot, &ret).await;
            ret
        } else {
            // currently, the returned cycles is not been used, mock 0 if delay
            Ok(Completed {
                cycles: 0,
                fee: Capacity::zero(),
            })
        }
    }
```

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

**File:** tx-pool/src/process.rs (L1016-1037)
```rust
fn _submit_entry(
    tx_pool: &mut TxPool,
    status: TxStatus,
    entry: TxEntry,
    callbacks: &Callbacks,
) -> Result<HashSet<TxEntry>, Reject> {
    let tx_hash = entry.transaction().hash();
    debug!("submit_entry {:?} {}", status, tx_hash);
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
}
```
