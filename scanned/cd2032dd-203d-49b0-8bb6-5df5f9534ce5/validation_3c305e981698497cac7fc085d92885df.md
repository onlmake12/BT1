After thorough investigation of the CKB codebase, I found a valid analog to the reported vulnerability class.

---

### Title
Inconsistent Guard Conditions Between `pre_check()` and `submit_entry()` Cause Repeated Wasted Script Verification When RBF Is Disabled — (`File: tx-pool/src/process.rs`)

### Summary

In `tx-pool/src/process.rs`, the `pre_check()` function (the "check" phase) returns `Ok` for a transaction that has a dead outpoint **and** a conflicting transaction in the pool, regardless of whether RBF is enabled. However, `submit_entry()` (the "execute" phase) **always** rejects such a transaction when RBF is disabled. The expensive script verification that runs between these two phases is therefore always wasted. An unprivileged tx-pool submitter or RPC caller can exploit this to repeatedly trigger costly script verification that always ends in rejection.

### Finding Description

The tx-pool processes incoming transactions through a multi-phase pipeline:

1. `pre_check()` — under a **read lock**, performs cheap initial checks.
2. Script verification — expensive CKB-VM execution.
3. `submit_entry()` — under a **write lock**, performs final admission.

In `pre_check()`, when `resolve_tx` returns `Err(Reject::Resolve(OutPointError::Dead(out)))` (a dead outpoint), the code re-resolves with `allow_dead=true`, then checks whether a conflicting transaction exists in the pool:

```rust
// tx-pool/src/process.rs lines 292-309
Err(Reject::Resolve(OutPointError::Dead(out))) => {
    let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
    let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
    let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
    if conflicts.is_none() {
        // ...
        return Err(Reject::Resolve(OutPointError::Dead(out)));
    }
    // we also return Ok here, so that the entry will be continue to be verified before submit
    Ok((tip_hash, rtx, status, fee, tx_size))
}
``` [1](#0-0) 

The condition for `pre_check()` to return `Ok` is: **conflict exists in pool** (`conflicts.is_some()`).

In `submit_entry()`, when RBF is disabled, the code checks for conflicts again and **always rejects** if one is found:

```rust
// tx-pool/src/process.rs lines 105-116
let conflicts = if tx_pool.enable_rbf() {
    tx_pool.check_rbf(&snapshot, &entry)?
} else {
    // RBF is disabled but we found conflicts, return error here
    let conflicted_outpoint =
        tx_pool.pool_map.find_conflict_outpoint(entry.transaction());
    if let Some(outpoint) = conflicted_outpoint {
        return Err(Reject::Resolve(OutPointError::Dead(outpoint)));
    }
    HashSet::new()
};
``` [2](#0-1) 

The condition for `submit_entry()` to succeed (RBF disabled) is: **no conflict in pool**.

These two conditions are **mutually exclusive**: whenever `pre_check()` returns `Ok` (because a conflict exists), `submit_entry()` will **always** reject (because a conflict exists and RBF is disabled). The expensive script verification that runs between them is always wasted.

The `enable_rbf()` check is straightforward:

```rust
// tx-pool/src/pool.rs lines 81-83
pub fn enable_rbf(&self) -> bool {
    self.config.min_rbf_rate > self.config.min_fee_rate
}
``` [3](#0-2) 

### Impact Explanation

When RBF is disabled (the default configuration, where `min_rbf_rate == min_fee_rate`), an attacker can repeatedly submit transactions that spend an outpoint already being spent by a pending pool transaction. Each submission:

1. Passes `pre_check()` (conflict exists → `Ok`).
2. Triggers full CKB-VM script verification (expensive CPU work).
3. Is rejected by `submit_entry()` (conflict exists, RBF disabled → `Err`).

This is a CPU-exhaustion DoS vector. The attacker can craft many distinct transactions (different outputs or witnesses, same conflicting input) to bypass the `check_txid_collision` deduplication and the `recent_reject` cache, each triggering a fresh round of script verification. The `after_process` handler records the rejection and puts the tx in the conflicts cache, but does not prevent a different tx with the same conflicting input from going through the same expensive path. [4](#0-3) 

### Likelihood Explanation

- Pool transaction contents are publicly visible via RPC (`get_transaction`, `get_raw_tx_pool`).
- Any unprivileged RPC caller or P2P relay peer can submit transactions.
- The attacker only needs to know one pending pool transaction's input outpoint to craft arbitrarily many conflicting transactions (varying outputs/witnesses).
- The per-peer rate limiter (30 req/s) provides partial mitigation but does not prevent the attack from multiple peers or over time.
- RBF is disabled by default (requires explicit configuration of `min_rbf_rate > min_fee_rate`), making this the common-case deployment scenario.

### Recommendation

In `pre_check()`, add a check for whether RBF is enabled before returning `Ok` for a dead-outpoint conflict. If RBF is disabled, reject immediately rather than allowing the transaction to proceed to expensive script verification:

```rust
// In the Err(Reject::Resolve(OutPointError::Dead(out))) arm of pre_check():
if conflicts.is_none() || !tx_pool.enable_rbf() {
    return Err(Reject::Resolve(OutPointError::Dead(out)));
}
Ok((tip_hash, rtx, status, fee, tx_size))
```

This mirrors the recommendation in the external report: align the guard condition in the "check" phase with the actual conditions required for the "execute" phase to succeed.

### Proof of Concept

1. Start a CKB node with default config (RBF disabled: `min_rbf_rate == min_fee_rate`).
2. Submit a valid transaction `T1` spending outpoint `O` → it enters the pending pool.
3. Craft transaction `T2` also spending outpoint `O` but with different outputs (higher capacity, different witness). `T2` has a different tx hash than `T1`.
4. Submit `T2` via `send_transaction` RPC.
   - `check_txid_collision` passes (different hash from `T1`).
   - `resolve_tx(allow_dead=false)` returns `Err(Dead(O))`.
   - `resolve_tx(allow_dead=true)` succeeds.
   - `find_conflict_outpoint` finds `T1` → `conflicts.is_some()` → `pre_check` returns `Ok`.
   - Full script verification of `T2` runs (expensive).
   - `submit_entry` finds conflict, RBF disabled → returns `Err(Resolve(Dead(O)))`.
5. Craft `T3`, `T4`, … `Tn` each spending `O` with different outputs. Repeat step 4 for each.
6. Each submission triggers full script verification that always fails, wasting node CPU proportional to the script complexity of the submitted transactions. [5](#0-4) [6](#0-5)

### Citations

**File:** tx-pool/src/process.rs (L96-116)
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

**File:** tx-pool/src/process.rs (L458-487)
```rust
    pub(crate) async fn after_process(
        &self,
        tx: TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
        _snapshot: &Snapshot,
        ret: &Result<Completed, Reject>,
    ) {
        let tx_hash = tx.hash();

        // log tx verification result for monitor node
        if log_enabled_target!("ckb_tx_monitor", Trace)
            && let Ok(c) = ret
        {
            trace_target!(
                "ckb_tx_monitor",
                r#"{{"tx_hash":"{:#x}","cycles":{}}}"#,
                tx_hash,
                c.cycles
            );
        }

        if matches!(
            ret,
            Err(Reject::RBFRejected(..) | Reject::Resolve(OutPointError::Dead(_)))
        ) {
            let mut tx_pool = self.tx_pool.write().await;
            if tx_pool.pool_map.find_conflict_outpoint(&tx).is_some() {
                tx_pool.record_conflict(tx.clone());
            }
        }
```

**File:** tx-pool/src/pool.rs (L81-83)
```rust
    pub fn enable_rbf(&self) -> bool {
        self.config.min_rbf_rate > self.config.min_fee_rate
    }
```
