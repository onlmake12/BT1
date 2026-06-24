Audit Report

## Title
Insufficient RBF Fee Validation Before Expensive Script Execution Enables CPU Exhaustion - (File: `tx-pool/src/process.rs`)

## Summary

In `tx-pool/src/process.rs`, the `pre_check` function allows RBF candidate transactions to proceed to full CKB-VM script execution after only verifying `fee >= min_fee_rate`. The actual RBF authorization rules — including the much stricter `min_replace_fee` threshold (Rule #3/#4), the unconfirmed-inputs constraint (Rule #2), and the replacement-count limit (Rule #5) — are only enforced in `submit_entry` under write lock, after script execution has already completed. An unprivileged attacker can exploit this ordering to force repeated full VM execution on transactions that are guaranteed to be rejected, causing CPU exhaustion on any node with RBF enabled.

## Finding Description

The `_process_tx` function in `tx-pool/src/process.rs` follows this sequence:

1. **`pre_check`** (read lock, lines 715–717): For a transaction whose input resolves as `Dead`, the RBF path is taken. It calls `check_tx_fee` (verifies only `fee >= min_fee_rate`) and `find_conflict_outpoint` (confirms a conflict exists), then returns `Ok`. The code comment at line 305 explicitly acknowledges this: *"we also return Ok here, so that the entry will be continue to be verified before submit"*.

2. **`verify_rtx`** (lines 724–732): Full `ContextualTransactionVerifier` execution, including CKB-VM lock/type script execution, runs unconditionally on the result of `pre_check`.

3. **`submit_entry`** (lines 753–754, write lock): Only here does `check_rbf` run (line 106), enforcing Rule #2 (no new unconfirmed inputs), Rule #3/#4 (`fee >= min_replace_fee = sum(replaced_fees) + extra_rbf_fee`), and Rule #5 (≤100 replacement candidates).

The critical gap: `check_tx_fee` checks `fee >= min_fee_rate` (e.g., 1000 shannons/KB), while `check_rbf` Rule #3/#4 requires `fee >= min_replace_fee`, which equals the sum of all replaced transactions' fees plus an additional RBF increment. These thresholds are entirely different. A transaction paying just above `min_fee_rate` but far below `min_replace_fee` passes `pre_check`, triggers full VM execution, and is only rejected in `submit_entry`.

**Root cause**: The design intentionally defers `check_rbf` to the write-lock phase to avoid concurrent state issues, but does not perform any partial RBF fee adequacy check in `pre_check` to gate expensive verification.

## Impact Explanation

This matches the allowed CKB bounty impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker can saturate the tx-pool's async verification workers by continuously submitting RBF-candidate transactions that pass `pre_check` but fail `check_rbf`. Each submission forces full CKB-VM script execution (potentially millions of cycles per transaction) at zero cost to the attacker beyond the RPC/P2P message. The verify queue workers (`verify_mgr.rs`) will be occupied processing these doomed transactions, degrading or blocking legitimate transaction processing on the targeted node.

## Likelihood Explanation

- RBF must be enabled (`min_rbf_rate > min_fee_rate`), which is a standard configurable option.
- The attacker only needs to identify one transaction currently in the pool, which is trivially observable via the `get_raw_tx_pool` RPC or P2P relay observation.
- No privileged access is required — any RPC caller or P2P peer can submit transactions.
- The `check_txid_collision` guard only blocks exact txid duplicates; the attacker can vary witnesses or outputs to generate fresh txids for each submission.
- The `verify_queue_contains` check is bypassed the same way.
- The attack is fully repeatable and cheap: the attacker pays only network/RPC overhead; the node pays full VM execution cost per attempt.

## Recommendation

Move a partial RBF fee adequacy check into `pre_check` under the read lock, before the transaction is enqueued for script verification. Specifically:

- Compute `min_replace_fee` using the conflicting transaction's fee (readable under the read lock) and reject early if `fee < min_replace_fee`.
- Optionally, also check Rule #2 (no new unconfirmed inputs) in `pre_check`, as this requires only a snapshot read.

The full atomicity-sensitive conflict resolution (Rule #5, descendant graph traversal) can remain in `submit_entry` under the write lock. The fee-adequacy and input-validity checks do not require write-lock exclusivity and can be evaluated cheaply upfront to prevent wasted VM cycles.

## Proof of Concept

1. Submit a high-fee transaction `tx_A` spending `cell_X` to the pool (enters pending state). Observe its fee, e.g., 10 CKB.
2. Craft `tx_B` spending the same `cell_X`, with fee = `min_fee_rate * size` (e.g., 363 shannons — far below `min_replace_fee` of ~10 CKB + extra RBF increment).
3. Submit `tx_B` via `send_transaction` RPC.
4. **`pre_check`**: `resolve_tx(..., rbf=true)` succeeds; `check_tx_fee` passes (363 ≥ min_fee); `find_conflict_outpoint` finds `tx_A` → returns `Ok`.
5. **`verify_rtx`**: Full CKB-VM script execution runs on `tx_B` (potentially millions of cycles).
6. **`submit_entry` → `check_rbf`**: Rule #3/#4: `363 < 10_CKB + extra` → `Err(RBFRejected)`.
7. `tx_B` is recorded in the conflicts pool; node has wasted full VM cycles.
8. Repeat steps 2–7 with fresh `tx_B` variants (different witnesses to change txid, bypassing `check_txid_collision`).
9. With sufficient submission rate, the verify queue workers are saturated, blocking legitimate transaction processing.