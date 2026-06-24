Audit Report

## Title
Missing Type Script `hash_type` Validation in `ScriptHashTypeVerifier` — (File: verification/src/transaction_verifier.rs)

## Summary
`ScriptHashTypeVerifier::verify()` iterates over every transaction output but validates only the lock script's `hash_type`, never the type script's `hash_type`. A transaction whose output carries a type script with a disallowed or unrecognized `hash_type` byte passes the non-contextual gate, enters the contextual verification pipeline, fails with a `Compatible` error (which is not classified as `is_malformed_tx()`), and leaves the submitting peer unbanned and free to resubmit indefinitely using the same UTXOs.

## Finding Description
In `verification/src/transaction_verifier.rs` lines 796–815, `ScriptHashTypeVerifier::verify()` loops over outputs and calls only `output.lock().hash_type()`:

```rust
for output in self.transaction.outputs() {
    if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
        ...
    }
    // output.type_().to_opt() is never consulted
}
```

`output.type_()` is never inspected. The struct comment at line 70 confirms the intent: "Check whether output **lock** hash type within enabled range" — the type-script side is explicitly absent.

`NonContextualTransactionVerifier::verify()` (lines 94–102) calls `self.script_hash_type.verify()` as the final cheap gate. `NonContextualBlockTxsVerifier::verify()` (lines 280–286) propagates the same gap to the block-processing path by calling `NonContextualTransactionVerifier` for every block transaction.

When a transaction with a valid lock `hash_type` but a disallowed type-script `hash_type` reaches contextual verification (`verify_rtx`), it fails with `TransactionError::Compatible`, which is explicitly excluded from `is_malformed_tx()` (lines 257–262 of `util/types/src/core/error.rs`):

```rust
TransactionError::Immature { .. }
| TransactionError::Compatible { .. }   // NOT malformed
| ...  => false,
```

Contrast this with `ScriptHashTypeNotPermitted` and `InvalidScriptHashType`, which ARE in `is_malformed_tx()` (lines 253–254) — but those errors are only reachable for lock scripts, never for type scripts, because the verifier never checks type scripts.

In `tx-pool/src/process.rs` lines 514–515, `ban_malformed` is called only when `reject.is_malformed_tx()` is true. Because `Compatible` is not malformed, the peer is never banned. The same logic applies in `non_contextual_verify` (lines 323–329): the early ban gate also only fires on `is_malformed_tx()`.

## Impact Explanation
This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker with any valid UTXO set can craft transactions whose type scripts carry a disallowed `hash_type` byte, submit them via RPC or P2P relay, and have each one processed through `pre_check` (UTXO resolution, database lookups) and into `verify_rtx` before rejection. Because the rejection is `Compatible` (not malformed), the peer is never banned. Because the transaction is rejected rather than committed, the input UTXOs are never consumed. The attacker can resubmit the same transaction indefinitely, occupying the verify queue and forcing repeated UTXO resolution work on the node without any cost beyond holding a small amount of CKB.

## Likelihood Explanation
No special privilege, key material, or hash power is required. Any caller with RPC access (`send_transaction`) or P2P relay access can craft the malformed type script. Setting a single byte field to a disallowed value is trivial. The attacker's CKB is never spent on rejection, making the attack essentially free to sustain. The node cannot distinguish this traffic from legitimate submissions and cannot ban the source.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to also validate `output.type_().to_opt()` when a type script is present, applying the same two-branch check already applied to lock scripts:

```rust
if let Some(type_script) = output.type_().to_opt() {
    if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_script.hash_type()) {
        let val: u8 = hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
        }
    } else {
        return Err(TransactionError::InvalidScriptHashType {
            hash_type: type_script.hash_type(),
        }.into());
    }
}
```

Update the struct comment and `NonContextualTransactionVerifier` doc comment to reflect that both lock and type script hash types are checked. No new error variants are needed; the existing `ScriptHashTypeNotPermitted` and `InvalidScriptHashType` variants (both already classified as `is_malformed_tx()`) are sufficient, ensuring the peer is banned on detection.

## Proof of Concept
1. Obtain any valid UTXO (any amount of CKB).
2. Construct a `TransactionView` spending that UTXO with one output whose lock script uses a valid, enabled `hash_type` (e.g., `0x01` / `Type`) and whose type script uses a `hash_type` byte absent from `ENABLED_SCRIPT_HASH_TYPE` (e.g., `0x05` if not yet enabled).
3. Submit via `send_transaction` RPC or P2P relay.
4. Observe: `NonContextualTransactionVerifier::verify()` returns `Ok(())` — the transaction enters the verify queue.
5. Observe: the transaction is rejected during contextual verification with a `Compatible` error; `is_malformed_tx()` returns `false`; the peer receives no ban.
6. Resubmit the identical transaction. The UTXO is still unspent; the node re-executes the full `pre_check` + `verify_rtx` pipeline on each submission without banning the source.
7. Automate submissions in a loop to saturate the verify queue and delay processing of legitimate transactions.