The code confirms the claim. Let me verify the full picture before rendering a verdict.

Audit Report

## Title
Missing `hash_type` Validation for Output Type Scripts in `ScriptHashTypeVerifier` — (File: verification/src/transaction_verifier.rs)

## Summary
`ScriptHashTypeVerifier::verify()` iterates over every transaction output and validates the lock script's `hash_type` against `ENABLED_SCRIPT_HASH_TYPE = {0, 1, 2, 4}`, but never inspects the type script. A type script with a disallowed `hash_type` (e.g., `Data3 = 6`) passes the entire non-contextual verification gate and is only rejected later during contextual script execution via `select_version()`, after the node has already expended resources on cell-dep resolution and script-group setup. This creates a zero-cost DoS amplification path and an inconsistency in `is_malformed_tx()` blame attribution.

## Finding Description
`ScriptHashTypeVerifier::verify()` loops over outputs and checks only `output.lock().hash_type()`:

```rust
// verification/src/transaction_verifier.rs  lines 796–814
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) { ... }
        } else { ... }
    }
    Ok(())
}
```

There is no call to `output.type_()` anywhere in this function (confirmed by grep). The `ENABLED_SCRIPT_HASH_TYPE` constant permits only `{0, 1, 2, 4}`:

```rust
// util/constant/src/consensus.rs  lines 7–11
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, 1u8, 2u8, 4u8,
};
```

The lower-level `check_data` path (`CellOutputReader::check_data`) does check both lock and type scripts, but only validates structural validity via `ScriptHashType::verify_value()` (even number or 1), not membership in the enabled set:

```rust
// util/gen-types/src/extension/check_data.rs  lines 24–27
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
// util/gen-types/src/core.rs  lines 39–41
pub fn verify_value(v: u8) -> bool {
    v.is_multiple_of(2) || v == 1
}
```

`Data3 = 6` is even, so it passes `check_data`. It also passes `ScriptHashTypeVerifier` (which never reads the type script). The transaction clears the entire non-contextual gate and proceeds to contextual verification, where `select_version()` finally rejects it:

```rust
// script/src/types.rs  lines 930–935
hash_type => {
    return Err(ScriptError::InvalidScriptHashType(format!(
        "The ScriptHashType/{:?} has not been activated...",
        hash_type
    )));
}
```

The doc comment at line 70 of `transaction_verifier.rs` explicitly says "Check whether output **lock** hash type within enabled range" — confirming the type script was never considered. The existing tests (`test_unknown_hash_type_output_lock`, `test_not_enabled_hash_type_output_lock`) exercise only lock-script paths with no analogous type-script tests.

Additionally, `is_malformed_tx()` marks `InvalidScriptHashType` and `ScriptHashTypeNotPermitted` as malformed (used for peer banning). Because the type-script path is never caught by the non-contextual verifier, the error surfaces as `ScriptError::InvalidScriptHashType` (not `TransactionError`), so `is_malformed_tx()` is never triggered for this case, creating an inconsistency in blame attribution for blocks containing such transactions.

## Impact Explanation
**High — bad design which could cause CKB network congestion with few costs.**

An attacker can craft transactions with a type script carrying `hash_type = 6, 8, 10, …, 254` (any even value ≥ 6 not in the enabled set). Each such transaction bypasses the cheap non-contextual gate and forces every receiving node to perform contextual verification: cell-dep resolution, script-group construction, and a `select_version()` call. Because rejected transactions pay no fees, the attacker's cost is bandwidth only, while each node pays the full contextual-verification overhead per transaction. This is a repeatable, zero-privilege amplification path for network-level DoS.

## Likelihood Explanation
Any unprivileged RPC caller or P2P relayer can trigger this. The `hash_type` field is a single byte in the serialized `Script` molecule struct. No key material, mining power, or social engineering is required. The attacker needs only valid UTXOs to construct the transaction inputs; conflicting transactions (same input, different type script) can be used to multiply the load across many nodes simultaneously.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's `hash_type` when present:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock script check
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err((TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            }).into());
        }

        // NEW: type script check
        if let Some(type_script) = output.type_().to_opt() {
            if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_script.hash_type()) {
                let val: u8 = hash_type.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
                }
            } else {
                return Err((TransactionError::InvalidScriptHashType {
                    hash_type: type_script.hash_type(),
                }).into());
            }
        }
    }
    Ok(())
}
```

Update the `NonContextualTransactionVerifier` doc comment (line 70) to mention type scripts, update the `ScriptHashTypeNotPermitted` and `InvalidScriptHashType` error messages to say "lock or type script" instead of "lock script", and add tests `test_unknown_hash_type_output_type` and `test_not_enabled_hash_type_output_type` mirroring the existing lock-script tests.

## Proof of Concept

```rust
// Craft a transaction whose output has a type script with hash_type = Data3 (6),
// which is NOT in ENABLED_SCRIPT_HASH_TYPE = {0, 1, 2, 4}.
let transaction = TransactionBuilder::default()
    .output(
        CellOutput::new_builder()
            .lock(Script::default()) // valid lock: Data (0)
            .type_(Some(
                Script::default()
                    .as_builder()
                    .hash_type(ScriptHashType::Data3) // disallowed: 6
                    .build()
            ).pack())
            .build(),
    )
    .build();

let verifier = ScriptHashTypeVerifier::new(&transaction);

// BUG: returns Ok(()) instead of Err(ScriptHashTypeNotPermitted { hash_type: 6 })
assert!(verifier.verify().is_ok());
```

The transaction passes `NonContextualTransactionVerifier` and proceeds to contextual verification, where `select_version()` rejects it with `ScriptError::InvalidScriptHashType`. The node has already paid the cost of cell-dep resolution and script-group setup. Repeating this with many transactions and many nodes constitutes a low-cost amplified DoS.