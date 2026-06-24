The code has been verified. All claims in the report are accurate. Let me confirm the key findings:Audit Report

## Title
Inconsistent RBF Guard Between `pre_check()` and `submit_entry()` Causes Guaranteed Wasted Script Verification — (`File: tx-pool/src/process.rs`)

## Summary

In `tx-pool/src/process.rs`, `pre_check()` returns `Ok` for any transaction that has a conflicting outpoint in the pool, regardless of whether RBF is enabled. However, `submit_entry()` unconditionally rejects such a transaction when RBF is disabled. The expensive CKB-VM script verification that executes between these two phases is therefore always wasted. An unprivileged attacker can repeatedly submit distinct transactions spending the same pending pool outpoint to exhaust node CPU with zero chance of success.

## Finding Description

The tx-pool processes transactions through `_process_tx()` in a three-phase pipeline:

1. **`pre_check()`** (read lock) — cheap initial checks
2. **`verify_rtx()`** — expensive CKB-VM script execution
3. **`submit_entry()`** (write lock) — final admission

In `_process_tx()`, the pipeline is:

```rust
// tx-pool/src/process.rs lines 715-753
let (ret, snapshot) = self.pre_check(&tx).await;
let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);
// ... expensive verify_rtx() runs here ...
let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
```

In `pre_check()`, when `resolve_tx(allow_dead=false)` returns `Err(Reject::Resolve(OutPointError::Dead(out)))`, the code re-resolves with `allow_dead=true` and checks for a conflict:

```rust
// tx-pool/src/process.rs lines 292-309
Err(Reject::Resolve(OutPointError::Dead(out))) => {
    let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
    let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
    let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
    if conflicts.is_none() {
        return Err(Reject::Resolve(OutPointError::Dead(out)));
    }
    Ok((tip_hash, rtx, status, fee, tx_size))  // returns Ok if conflict exists
}
```

`pre_check()` returns `Ok` **if and only if** a conflict exists in the pool.

In `submit_entry()`, when RBF is disabled:

```rust
// tx-pool/src/process.rs lines 105-116
let conflicts = if tx_pool.enable_rbf() {
    tx_pool.check_rbf(&snapshot, &entry)?
} else {
    let conflicted_outpoint =
        tx_pool.pool_map.find_conflict_outpoint(entry.transaction());
    if let Some(outpoint) = conflicted_outpoint {
        return Err(Reject::Resolve(OutPointError::Dead(outpoint)));
    }
    HashSet::new()
};
```

`submit_entry()` rejects **if and only if** a conflict exists in the pool (when RBF is disabled).

`enable_rbf()` is:

```rust
// tx-pool/src/pool.rs lines 81-83
pub fn enable_rbf(&self) -> bool {
    self.config.min_rbf_rate > self.config.min_fee_rate
}
```

The default configuration has `min_rbf_rate == min_fee_rate`, so RBF is disabled by default. The two conditions are **mutually exclusive**: `pre_check()` passes exactly when `submit_entry()` will reject. Every transaction that clears `pre_check()` via the dead-outpoint path will trigger full script verification and then be unconditionally rejected by `submit_entry()` when RBF is disabled.

The `recent_reject` cache (keyed by tx hash) and `check_txid_collision` (also keyed by tx hash) provide no protection because each crafted transaction has a distinct hash. The `txs_verify_cache` is keyed by `witness_hash`; varying the witness across submissions bypasses it entirely.

## Impact Explanation

This is a **High** severity finding matching: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

An attacker can force any CKB node to perform unbounded CKB-VM script verification work at negligible cost. The attacker crafts transactions spending a known pending pool outpoint with varying outputs or witnesses (each producing a unique tx hash and witness hash). Each submission:
- Passes all deduplication guards
- Passes `pre_check()` (conflict found → `Ok`)
- Triggers full CKB-VM script execution (CPU-proportional to script complexity)
- Is rejected by `submit_entry()` (conflict found, RBF disabled → `Err`)

Since the tx-pool verification workers are shared with legitimate transaction processing, sustained attack degrades the node's ability to process real transactions, propagate blocks, and participate in the network. Applied simultaneously to multiple nodes, this constitutes network-wide congestion.

## Likelihood Explanation

- Pool contents are publicly visible via `get_raw_tx_pool` and `get_transaction` RPC — no privileged access needed to identify a target outpoint.
- Any unprivileged RPC caller or P2P relay peer can submit transactions.
- RBF is disabled in the default configuration (`min_rbf_rate == min_fee_rate`), making this the common deployment scenario.
- The attacker cost per submission is trivial: construct a transaction with a different output capacity or witness. No valid signature is required for the attack to consume verification CPU (the script runs before the signature check fails or succeeds).
- The P2P rate limiter (30 req/s per peer per message type) applies only to relay messages, not to RPC `send_transaction` calls, which have no rate limit.
- The attack is indefinitely repeatable as long as the target transaction remains in the pool.

## Recommendation

In `pre_check()`, add an `enable_rbf()` guard before returning `Ok` in the dead-outpoint conflict branch. If RBF is disabled, reject immediately rather than proceeding to script verification:

```rust
// In the Err(Reject::Resolve(OutPointError::Dead(out))) arm of pre_check():
if conflicts.is_none() || !tx_pool.enable_rbf() {
    return Err(Reject::Resolve(OutPointError::Dead(out)));
}
Ok((tip_hash, rtx, status, fee, tx_size))
```

This aligns the `pre_check()` guard with the actual admission condition in `submit_entry()`, eliminating the wasted verification work.

## Proof of Concept

1. Start a CKB node with default config (`min_rbf_rate == min_fee_rate`, RBF disabled).
2. Submit a valid transaction `T1` spending outpoint `O` → it enters the pending pool.
3. Query `get_raw_tx_pool` to confirm `T1` is pending and identify outpoint `O`.
4. Craft transaction `T2` spending `O` with a different output capacity (different tx hash, different witness hash). Submit via `send_transaction` RPC.
   - `check_txid_collision`: passes (different hash).
   - `resolve_tx(allow_dead=false)`: returns `Err(Dead(O))`.
   - `resolve_tx(allow_dead=true)`: succeeds.
   - `find_conflict_outpoint`: finds `T1` → `conflicts.is_some()` → `pre_check` returns `Ok`.
   - `verify_rtx()`: full CKB-VM script execution runs.
   - `submit_entry()`: finds conflict, RBF disabled → returns `Err(Resolve(Dead(O)))`.
5. Craft `T3`, `T4`, … `Tn` each spending `O` with distinct outputs/witnesses. Repeat step 4 for each.
6. Observe node CPU consumption proportional to script complexity × number of submissions. Each submission triggers a full verification cycle that always ends in rejection. Monitor via node metrics (`ckb_tx_pool_sync_process` / `ckb_tx_pool_async_process`) to confirm sustained verification load with zero successful admissions.