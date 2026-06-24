The code confirms the claim. The `ScriptHashTypeVerifier::verify()` implementation at lines 796–814 only iterates over `output.lock().hash_type()` and never calls `output.type_().to_opt()`. The `ENABLED_SCRIPT_HASH_TYPE` set is `{0, 1, 2, 4}`. The `ScriptHashType` enum is generated via `seq!(N in 3..=127 { Data~N = N << 1 })`, so `Data3 = 6` is a structurally valid variant that passes `TryInto` but is not in the enabled set. `select_version()` in `script/src/types.rs` lines 930–935 rejects it with `InvalidScriptHashType` at spend time.

Audit Report

## Title
`ScriptHashTypeVerifier` Fails to Validate Type Script Hash Type in Transaction Outputs, Enabling Permanently Unspendable Cells — (File: `verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates only the lock script's `hash_type` against `ENABLED_SCRIPT_HASH_TYPE`. The type script's `hash_type` is never checked. An output carrying a type script with a structurally valid but non-permitted hash type (e.g., `Data3 = 6`) passes all non-contextual verification and is committed to the chain. Any subsequent transaction attempting to spend that output fails at script execution with `InvalidScriptHashType`, permanently locking the capacity in the cell.

## Finding Description
In `verification/src/transaction_verifier.rs` lines 796–814, `ScriptHashTypeVerifier::verify()` loops over outputs and performs:

```rust
if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
    let val: u8 = hash_type.into();
    if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) { ... }
} else { ... }
```

`output.type_().to_opt()` is never consulted. The `ScriptHashType` enum (`util/gen-types/src/core.rs` lines 9–32) is generated with `seq!(N in 3..=127 { Data~N = N << 1 })`, making `Data3 = 6` a valid enum variant. `TryInto::<ScriptHashType>::try_into(6u8)` succeeds, so the lock-script path would catch `6` — but the type script path is entirely absent.

`ENABLED_SCRIPT_HASH_TYPE` (`util/constant/src/consensus.rs` lines 7–11) is `{0, 1, 2, 4}`. Value `6` is not present.

When the output is later consumed, `select_version()` (`script/src/types.rs` lines 900–936) calls `ScriptHashType::try_from(script.hash_type())` on the type script. `Data3` falls through to the catch-all arm (lines 930–935) and returns `Err(ScriptError::InvalidScriptHashType(...))`, causing the spending transaction to be rejected. Because the creating transaction was already committed, the cell is permanently unspendable.

## Impact Explanation
Any user can submit a transaction via the `send_transaction` RPC with an output whose type script carries `hash_type = 6` (or any even value ≥ 6 not in `ENABLED_SCRIPT_HASH_TYPE`). The `NonContextualTransactionVerifier` (lines 71–102) runs `ScriptHashTypeVerifier` and accepts the transaction. The output is committed to the chain. The capacity locked in that cell is irrecoverable. This constitutes permanent, irreversible loss of CKB capacity for the cell owner, matching the allowed impact: **vulnerabilities which could easily damage CKB economy**.

## Likelihood Explanation
The exploit path is fully unprivileged — any user submitting a transaction via RPC can trigger it. No special permissions or keys beyond ownership of the input cells are required. The scenario is realistic for developers experimenting with future VM versions (e.g., Data3/Data4 hash types) or for users of SDKs/tooling that do not pre-validate hash types before submission. The bug is repeatable and deterministic.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type for each output:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // Existing lock script check
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err(TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            }.into());
        }

        // Add type script check
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
    }
    Ok(())
}
```

## Proof of Concept
1. Construct a transaction with one output: lock script `hash_type = 0` (Data, valid), type script `hash_type = 6` (Data3, not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Submit via `send_transaction` RPC.
3. `ScriptHashTypeVerifier::verify()` checks only the lock script hash type (`0`), which passes. Transaction is accepted and committed.
4. Construct a second transaction spending that output.
5. `select_version()` is called on the type script with `hash_type = 6`. `ScriptHashType::try_from(6)` returns `Data3`, which hits the catch-all arm at `script/src/types.rs` lines 930–935 and returns `Err(ScriptError::InvalidScriptHashType(...))`.
6. The spending transaction is rejected. The output is permanently unspendable. Capacity is lost.

A unit test can be added to `verification/src/transaction_verifier.rs` constructing such a transaction and asserting that `ScriptHashTypeVerifier::verify()` currently returns `Ok(())` (demonstrating the bug) and returns `Err(ScriptHashTypeNotPermitted)` after the fix.