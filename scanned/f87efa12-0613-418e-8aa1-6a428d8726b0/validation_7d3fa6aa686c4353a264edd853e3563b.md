Audit Report

## Title
Insufficient RBF Fee Validation Before Expensive Script Execution Enables CPU Exhaustion - (File: `tx-pool/src/process.rs`)

## Summary

In `tx-pool/src/process.rs`, the `pre_check` function for RBF-path transactions validates only `fee >= min_fee_rate` before returning `Ok`, allowing the transaction to proceed to full CKB-VM script execution via `verify_rtx`. The actual RBF authorization rules — including the much stricter `min_replace_fee` threshold (= sum of replaced tx fees + extra RBF fee), Rule #2 (no new unconfirmed inputs), and Rule #5 (≤100 replacement candidates) — are only enforced in `submit_entry` via `check_rbf`, which runs after script execution completes. An unprivileged attacker can repeatedly submit low-fee transactions that conflict with a known pool transaction, forcing full VM execution (up to 70M cycles each) before the node rejects them, with no cost to the attacker.

## Finding Description

**Exact code path:**

In `_process_tx` (`tx-pool/src/process.rs` L705–753), the execution order is:

1. `pre_check` (read lock) — for the RBF path (`OutPointError::Dead`), only `check_tx_fee` and `find_conflict_outpoint` are called, then `Ok` is returned.
2. `verify_rtx` — full `ContextualTransactionVerifier` including `ScriptVerifier` (CKB-VM) runs.
3. `submit_entry` (write lock) — `check_rbf` is called here, enforcing `min_replace_fee`, Rule #2, and Rule #5.

**Root cause — `pre_check` RBF branch** (`tx-pool/src/process.rs` L292–309):
```rust
Err(Reject::Resolve(OutPointError::Dead(out))) => {
    let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
    let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;  // only checks fee >= min_fee_rate
    let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
    if conflicts.is_none() { ... return Err(...); }
    Ok((tip_hash, rtx, status, fee, tx_size))  // proceeds to VM execution
}
```

`check_tx_fee` (`tx-pool/src/util.rs` L45–52) only checks `fee >= min_fee_rate.fee(tx_size)` — the absolute minimum bar (1000 shannons/KB by default).

**The deferred check — `check_rbf`** (`tx-pool/src/pool.rs` L574–679) enforces:
- Rule #2: no new unconfirmed inputs (L602–609)
- Rule #5: ≤100 replacement candidates (L619–623)
- Rules #3/#4: `fee >= min_replace_fee = sum(replaced_txs.fee) + min_rbf_rate * size` (L665–676)

`calculate_min_replace_fee` (`tx-pool/src/pool.rs` L101–127) shows `min_replace_fee` includes the full fee of all replaced transactions — orders of magnitude higher than `min_fee_rate * size` when the replaced tx has any meaningful fee.

**Full VM execution** (`tx-pool/src/util.rs` L101–115) runs `ContextualTransactionVerifier::verify_with_pause` up to `max_tx_verify_cycles = 70_000_000` cycles before `check_rbf` is ever called.

## Impact Explanation

This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker can saturate the verify queue (256MB limit, `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`) with RBF-candidate transactions that each consume up to 70M VM cycles before being rejected. The verify queue being full causes legitimate transactions to receive `Reject::Full`, blocking normal pool operation. The attacker pays zero fee (transactions are rejected before entering the pool) while the node bears the full CPU cost of script execution. With multiple workers processing the queue concurrently, sustained CPU exhaustion is achievable, degrading or crashing the node's transaction processing capability.

## Likelihood Explanation

- RBF is enabled by default in production config (`min_fee_rate = 1000`, `min_rbf_rate = 1500` in `resource/ckb.toml` L212–214), satisfying `enable_rbf()` which checks `min_rbf_rate > min_fee_rate`.
- Any pool transaction is observable via the `get_raw_tx_pool` RPC or P2P relay observation — no privileged access required.
- The attacker only needs to vary witnesses or outputs to change the txid and bypass `check_txid_collision`, making the attack indefinitely repeatable.
- The cost per attack iteration is a single RPC/P2P message with a minimal fee (363 shannons for a small tx vs. potentially millions of shannons for `min_replace_fee`).

## Recommendation

Move the core RBF fee adequacy check into `pre_check` before the transaction is enqueued for script verification. Specifically, within the `Err(Reject::Resolve(OutPointError::Dead(out)))` branch of `pre_check`, after `find_conflict_outpoint` confirms a conflict exists, compute a preliminary `min_replace_fee` estimate using the conflicting transaction's fee (readable under the existing read lock) and reject early if `fee < preliminary_min_replace_fee`. Rule #2 (no new unconfirmed inputs against the snapshot) can also be checked cheaply under the read lock. Structural checks requiring write-lock atomicity (final conflict set resolution) can remain in `submit_entry`.

## Proof of Concept

1. Submit `tx_A` spending `cell_X` with fee = 10 CKB to the pool. Observe it enters pending state.
2. Observe `tx_A`'s txid via `get_raw_tx_pool` RPC.
3. Craft `tx_B` spending the same `cell_X`, with fee = `min_fee_rate * size` (e.g., 363 shannons — far below `min_replace_fee` of ~10 CKB + extra).
4. Submit `tx_B` via `send_transaction` RPC.
5. **`pre_check`**: `resolve_tx(..., rbf=true)` succeeds; `check_tx_fee` passes (363 ≥ min_fee); `find_conflict_outpoint` finds `tx_A` → returns `Ok`.
6. **`verify_rtx`**: Full CKB-VM script execution runs on `tx_B` (up to 70M cycles consumed).
7. **`submit_entry` → `check_rbf`**: Rule #3/#4: `363 < 10_CKB + extra` → `Err(RBFRejected)`.
8. `tx_B` is added to conflicts pool; node wasted full VM cycles.
9. Repeat steps 3–8 with fresh `tx_B` variants (different witnesses to change txid). Each iteration forces full VM execution at zero cost to the attacker.
10. With sufficient submission rate, the 256MB verify queue fills, causing `Reject::Full` for all legitimate transactions.