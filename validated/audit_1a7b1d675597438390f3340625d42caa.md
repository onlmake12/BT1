Audit Report

## Title
Inconsistent RBF Guard Between `pre_check()` and `submit_entry()` Causes Guaranteed Wasted Script Verification When RBF Is Disabled — (`File: tx-pool/src/process.rs`)

## Summary

In `tx-pool/src/process.rs`, `pre_check()` returns `Ok` for any transaction that spends a dead outpoint and has a conflicting transaction in the pool, without checking whether RBF is enabled. `submit_entry()` unconditionally rejects such transactions when RBF is disabled. The expensive CKB-VM script verification that executes between these two phases is therefore always wasted. An unprivileged attacker can repeatedly submit distinct transactions spending the same pending-pool outpoint to trigger unbounded CPU-intensive script verification that always ends in rejection.

## Finding Description

The transaction processing pipeline in `_process_tx()` is:

1. `pre_check()` — read lock, cheap checks
2. `verify_rtx()` — expensive CKB-VM script execution
3. `submit_entry()` — write lock, final admission

In `pre_check()`, the `Err(Reject::Resolve(OutPointError::Dead(out)))` arm re-resolves with `allow_dead=true`, then checks only whether a conflicting transaction exists in the pool. If one exists, it returns `Ok` unconditionally — no check for `enable_rbf()`:

```rust
// tx-pool/src/process.rs lines 292-309
Err(Reject::Resolve(OutPointError::Dead(out))) => {
    let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
    let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
    let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
    if conflicts.is_none() {
        return Err(Reject::Resolve(OutPointError::Dead(out)));
    }
    Ok((tip_hash, rtx, status, fee, tx_size))  // ← always Ok when conflict exists
}
```

In `submit_entry()`, when RBF is disabled, any conflict causes an immediate rejection:

```rust
// tx-pool/src/process.rs lines 105-116
let conflicts = if tx_pool.enable_rbf() {
    tx_pool.check_rbf(&snapshot, &entry)?
} else {
    let conflicted_outpoint =
        tx_pool.pool_map.find_conflict_outpoint(entry.transaction());
    if let Some(outpoint) = conflicted_outpoint {
        return Err(Reject::Resolve(OutPointError::Dead(outpoint)));  // ← always Err
    }
    HashSet::new()
};
```

`enable_rbf()` returns `true` only when `min_rbf_rate > min_fee_rate`, which is not the default:

```rust
// tx-pool/src/pool.rs lines 81-83
pub fn enable_rbf(&self) -> bool {
    self.config.min_rbf_rate > self.config.min_fee_rate
}
```

The two conditions are mutually exclusive when RBF is disabled: `pre_check()` passes iff a conflict exists; `submit_entry()` fails iff a conflict exists. The full `verify_rtx()` call at line 724 always runs between them and is always wasted.

The `txs_verify_cache` is keyed by `witness_hash`. Since the cache update at lines 758–765 is only reached on success (the `try_or_return_with_snapshot!` macro at line 754 returns early on `submit_entry` failure), each distinct transaction with a different witness triggers a full re-verification with no cache benefit. The `recent_reject` cache and `check_txid_collision` are both keyed by `tx_hash`, so distinct transactions (different outputs/witnesses) bypass both.

## Impact Explanation

This is a **High** severity finding matching: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

An attacker can craft arbitrarily many transactions spending the same pending-pool outpoint (varying outputs or witnesses to produce distinct tx hashes), submit them via the `send_transaction` RPC or P2P relay, and force the target node to execute full CKB-VM script verification for each one. The CPU cost scales with script complexity and submission rate. Since pool contents are publicly visible via `get_raw_tx_pool`, the attacker can identify suitable target outpoints on any node. Targeting multiple relay nodes simultaneously degrades the network's transaction processing capacity.

## Likelihood Explanation

- Pool transaction inputs are publicly visible via RPC (`get_raw_tx_pool`, `get_transaction`).
- Any unprivileged RPC caller or P2P peer can submit transactions.
- RBF is disabled by default (`min_rbf_rate == min_fee_rate`), making this the common deployment scenario.
- The `send_transaction` RPC has no rate limiter. The P2P relay rate limiter is 30 req/s per peer, but is trivially bypassed by using multiple peers.
- The attacker needs only one pending pool transaction's input outpoint to craft unlimited conflicting transactions.
- The `recent_reject` and `txs_verify_cache` caches provide no protection since each crafted transaction has a distinct tx hash and witness hash.

## Recommendation

In `pre_check()`, add an `enable_rbf()` check before returning `Ok` in the dead-outpoint conflict arm. If RBF is disabled, reject immediately:

```rust
// In the Err(Reject::Resolve(OutPointError::Dead(out))) arm:
if conflicts.is_none() || !tx_pool.enable_rbf() {
    return Err(Reject::Resolve(OutPointError::Dead(out)));
}
Ok((tip_hash, rtx, status, fee, tx_size))
```

This aligns the guard condition in the "check" phase with the actual admission condition in the "execute" phase, eliminating the wasted verification work.

## Proof of Concept

1. Start a CKB node with default config (`min_rbf_rate == min_fee_rate`, RBF disabled).
2. Submit a valid transaction `T1` spending outpoint `O` → it enters the pending pool.
3. Craft transaction `T2` spending `O` with different outputs (e.g., higher capacity) and/or a different witness. `T2` has a different tx hash and witness hash from `T1`.
4. Submit `T2` via `send_transaction` RPC:
   - `check_txid_collision` passes (different hash).
   - `resolve_tx(allow_dead=false)` → `Err(Dead(O))`.
   - `resolve_tx(allow_dead=true)` → succeeds.
   - `find_conflict_outpoint` finds `T1` → `conflicts.is_some()` → `pre_check` returns `Ok`.
   - Full CKB-VM script verification of `T2` executes (expensive).
   - `submit_entry` finds conflict, RBF disabled → `Err(Resolve(Dead(O)))`.
5. Craft `T3`, `T4`, … `Tn` each spending `O` with distinct outputs/witnesses. Repeat step 4 for each.
6. Each submission triggers full script verification that always fails. CPU consumption scales linearly with the number of submissions and the script complexity of the submitted transactions. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** tx-pool/src/process.rs (L105-116)
```rust
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

**File:** tx-pool/src/process.rs (L292-309)
```rust
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
```

**File:** tx-pool/src/process.rs (L715-754)
```rust
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
        try_or_return_with_snapshot!(ret, submit_snapshot);
```

**File:** tx-pool/src/process.rs (L758-765)
```rust
        if verify_cache.is_none() {
            // update cache
            let txs_verify_cache = Arc::clone(&self.txs_verify_cache);
            tokio::spawn(async move {
                let mut guard = txs_verify_cache.write().await;
                guard.put(wtx_hash, verified);
            });
        }
```

**File:** tx-pool/src/pool.rs (L81-83)
```rust
    pub fn enable_rbf(&self) -> bool {
        self.config.min_rbf_rate > self.config.min_fee_rate
    }
```
