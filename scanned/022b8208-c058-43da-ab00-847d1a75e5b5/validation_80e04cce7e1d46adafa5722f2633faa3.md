Audit Report

## Title
Missing Type Script Hash Type Validation in `ScriptHashTypeVerifier` - (File: `verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over every transaction output but only validates `output.lock().hash_type()` against `ENABLED_SCRIPT_HASH_TYPE`. The `output.type_()` script's `hash_type` field is never inspected. A transaction carrying a type script with a future `ScriptHashType` (e.g., `Data3` = 6) passes the non-contextual verifier and is admitted to contextual verification, where it fails only after cell resolution and script-group construction — work that should have been short-circuited at the cheap admission gate.

## Finding Description

`ScriptHashTypeVerifier::verify()` at lines 796–814 of `verification/src/transaction_verifier.rs` loops over outputs and checks only the lock script:

```rust
for output in self.transaction.outputs() {
    if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
        ...
    }
    // output.type_() is never examined
}
```

The upstream structural gate `check_data()` in `util/gen-types/src/extension/check_data.rs` (lines 10–27) calls `ScriptHashType::verify_value()`, which accepts any even byte or `1`. Since `Data3 = 6` is even, it passes `check_data()`. The `ScriptHashType` enum in `util/gen-types/src/core.rs` (lines 9–32) defines all 128 variants via `seq!`, so `TryInto::<ScriptHashType>::try_into(6u8)` succeeds and returns `ScriptHashType::Data3`. The value `6` is not in `ENABLED_SCRIPT_HASH_TYPE` (`{0, 1, 2, 4}`), but because the type script is never checked, no error is raised.

The transaction then proceeds through `tx-pool/src/util.rs` `non_contextual_verify()` → fee check → `verify_rtx()` → `ContextualTransactionVerifier`. Inside contextual verification, `SgData::new()` calls `tx_data.select_version(&script_group.script)` (line 989 of `script/src/types.rs`), which hits the catch-all arm at line 930–935 and returns `ScriptError::InvalidScriptHashType`. This is the first point of rejection — after cell resolution and script-group construction have already been performed.

The test `test_not_enabled_hash_type_output_lock` (lines 100–122 of `verification/src/tests/transaction_verifier.rs`) confirms the lock-script path is covered. No equivalent test exists for the type-script path, confirming the gap.

## Impact Explanation

This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** The non-contextual verifier is the cheap O(1) admission gate; contextual verification requires resolving cell references and building script groups before the error is surfaced. An attacker paying minimum fees can force every receiving node to perform this extra work for each crafted transaction. Additionally, the rejection error originates from `ScriptError::InvalidScriptHashType` rather than `TransactionError::ScriptHashTypeNotPermitted`, producing incorrect error provenance for tooling and integrators.

## Likelihood Explanation

Any unprivileged transaction sender or P2P relay peer can trigger this. Setting `hash_type = 6` in a type script is a single-byte change to a serialized script. The attacker needs only valid inputs (to pass fee and resolve checks) and can repeat the attack at minimum-fee cost. No key material, mining power, or privileged access is required.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type for every output that carries one:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        check_hash_type(output.lock().hash_type())?;
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}
```

Add a corresponding test mirroring `test_not_enabled_hash_type_output_lock` for the type-script field.

## Proof of Concept

1. Build a transaction with a valid lock script (`Data = 0`) and a type script with `hash_type = ScriptHashType::Data3` (value `6`).
2. Call `ScriptHashTypeVerifier::new(&tx).verify()` — it returns `Ok(())` because only the lock script is checked.
3. The existing test `test_not_enabled_hash_type_output_lock` (line 101) proves the same value `6` in the lock script is correctly rejected with `TransactionError::ScriptHashTypeNotPermitted`. No equivalent test for the type script exists.
4. Submit the transaction via `send_transaction` RPC with sufficient fee. It passes `non_contextual_verify()`, passes the fee check, enters `verify_rtx()`, and is rejected only inside `ContextualTransactionVerifier` when `select_version()` hits the catch-all arm — with `ScriptError::InvalidScriptHashType` instead of `TransactionError::ScriptHashTypeNotPermitted`.