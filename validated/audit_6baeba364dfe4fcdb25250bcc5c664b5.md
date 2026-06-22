### Title
TOCTOU Race in Tx-Pool Admission Allows Redundant Script Verification — (File: `tx-pool/src/process.rs`)

---

### Summary

The tx-pool admission flow in `TxPoolService::_process_tx` performs the `check_txid_collision` guard under a **read lock** in `pre_check`, releases that lock, runs expensive script verification (`verify_rtx`) with **no lock held**, then acquires a **write lock** in `submit_entry`. The write-lock path does not re-verify txid collision. This is a direct TOCTOU analog to the reentrancy class: "check" and "effect" are separated by an unbounded async window, and the check is not re-done atomically with the effect.

---

### Finding Description

**Root cause — `_process_tx` (tx-pool/src/process.rs, lines 705–777):**

The function executes three distinct, non-atomic phases:

**Phase 1 — `pre_check` (read lock acquired, then released):** [1](#0-0) 

Inside `pre_check` (lines 276–316), `check_txid_collision` is called under the read lock to verify the tx is not already in the pool. The read lock is dropped when `pre_check` returns. [2](#0-1) 

**Phase 2 — `verify_rtx` (no lock held):** [3](#0-2) 

This is the expensive script execution step. It runs entirely without any lock, and for complex scripts can consume the full `max_block_cycles` budget.

**Phase 3 — `submit_entry` (write lock acquired):** [4](#0-3) 

Inside `submit_entry`, the developer comment at line 104 explicitly acknowledges the concurrency hazard for RBF and moves that check under the write lock: [5](#0-4) 

The snapshot-tip change guard at lines 119–134 re-runs `check_rtx` and `time_relative_verify` **only if the tip changed**: [6](#0-5) 

**`check_txid_collision` is never re-executed under the write lock.** If the snapshot tip has not advanced (no new block between Phase 1 and Phase 3), the duplicate-tx guard is entirely absent in Phase 3.

**Secondary TOCTOU — `resumeble_process_tx` (lines 335–353):**

Three separate lock acquisitions with no atomicity guarantee: [7](#0-6) 

`orphan_contains` acquires and releases the orphan read lock; `verify_queue_contains` acquires and releases the verify-queue read lock; `enqueue_verify_queue` acquires the verify-queue write lock. Between any two of these, a concurrent task can insert the same tx, making the preceding check stale.

---

### Impact Explanation

An unprivileged RPC caller (`send_transaction`) or P2P relay peer can submit the same transaction N times concurrently. Each submission independently passes `pre_check` (the pool is empty of that txid at read-lock time), and all N copies proceed into `verify_rtx`. For a transaction whose script consumes cycles near `max_block_cycles`, this causes N-fold redundant CPU consumption on the node for a single logical transaction. The attacker pays only the cost of N network/RPC calls; the node pays N full script-execution cycles. This is a realistic resource-exhaustion vector that degrades block-assembly and tx-relay throughput.

---

### Likelihood Explanation

**Medium.** The attack requires no special privilege — any RPC caller or connected P2P peer qualifies. Concurrent submission of the same transaction is trivially automated (e.g., N parallel HTTP calls to `send_transaction`). The window is wide: `verify_rtx` for a near-limit script can take hundreds of milliseconds, giving ample time for concurrent submissions to all pass `pre_check` before any one of them completes Phase 3.

---

### Recommendation

1. **Re-check `check_txid_collision` inside `submit_entry` under the write lock**, mirroring the explicit treatment of `check_rbf`: [8](#0-7) 

2. **Collapse the three non-atomic lock acquisitions in `resumeble_process_tx`** into a single write-lock acquisition that atomically checks both `orphan_contains` and `verify_queue_contains` before enqueuing.

---

### Proof of Concept

```
1. Craft transaction T with a script consuming ~max_block_cycles.
2. Open N concurrent connections to the node's RPC endpoint.
3. Send `send_transaction(T)` from all N connections simultaneously.
4. Each call enters _process_tx → pre_check (read lock):
     check_txid_collision → T not in pool → OK
     read lock released
5. All N calls enter verify_rtx concurrently (no lock held).
   → Node executes T's script N times in parallel.
6. First call acquires write lock in submit_entry → T added to pool.
7. Remaining N-1 calls acquire write lock sequentially:
     pre_resolve_tip == tip_hash (no new block) → check_rtx skipped
     check_txid_collision NOT called
     _submit_entry → pool_map.add_entry handles duplicate at storage level,
     but verify_rtx already ran N-1 extra times.
Result: O(N × max_block_cycles) CPU consumed for one transaction admission.
```

The structural parallel to the reported `Pool.sol` reentrancy is exact: state is read (collision check), an external/async operation executes (script verification), and the state is mutated (pool insertion) without re-validating the original guard — allowing the operation to proceed multiple times against a guard that was only enforced once.

### Citations

**File:** tx-pool/src/process.rs (L103-116)
```rust
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
```

**File:** tx-pool/src/process.rs (L119-134)
```rust
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

**File:** tx-pool/src/process.rs (L276-316)
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
        (ret, snapshot)
    }
```

**File:** tx-pool/src/process.rs (L344-353)
```rust
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

**File:** tx-pool/src/process.rs (L715-717)
```rust
        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);
```

**File:** tx-pool/src/process.rs (L724-734)
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
```

**File:** tx-pool/src/process.rs (L753-754)
```rust
        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
        try_or_return_with_snapshot!(ret, submit_snapshot);
```
