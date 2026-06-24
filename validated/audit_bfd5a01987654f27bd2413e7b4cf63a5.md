Audit Report

## Title
`ScriptHashTypeVerifier::verify` Omits Type-Script `hash_type` Validation, Allowing Not-Permitted Hash Types to Bypass Non-Contextual Checks — (`File: verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates the `hash_type` of each output's **lock script** against `ENABLED_SCRIPT_HASH_TYPE = {0, 1, 2, 4}`, but never inspects the `hash_type` of each output's **type script**. A transaction whose output carries `type_script.hash_type = 6` (`Data3`) silently passes the entire `NonContextualTransactionVerifier` pipeline and is only rejected later during the more expensive contextual verification stage, after cell resolution and script-group construction have already been performed.

## Finding Description
`ScriptHashTypeVerifier::verify()` (lines 796–814 of `verification/src/transaction_verifier.rs`) loops over outputs and checks only `output.lock().hash_type()`:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(...ScriptHashTypeNotPermitted...);
            }
        } else { ... }
        // output.type_().to_opt() is never inspected
    }
    Ok(())
}
```

`ENABLED_SCRIPT_HASH_TYPE` (`util/constant/src/consensus.rs` lines 7–11) is `{0, 1, 2, 4}`.

`ScriptHashType` is generated for every even value 0–254 plus 1 (`Type`) via `seq!(N in 3..=127 { Data~N = N << 1 })` (`util/gen-types/src/core.rs` lines 9–32). `Data3 = 6`, `Data4 = 8`, etc. are all structurally valid — `ScriptHashType::verify_value(6)` returns `true` because `6.is_multiple_of(2)` — so they pass the earlier `check_data` gate (`util/gen-types/src/extension/check_data.rs` lines 10–27) as well.

The downstream `select_version` (`script/src/types.rs` lines 930–935) does catch these values and returns `ScriptError::InvalidScriptHashType`, but only after the node has already performed cell resolution and script-group construction. The `NonContextualTransactionVerifier` comment itself documents the gap: "Check whether output lock hash type within enabled range" — type script is omitted.

The existing test suite (`verification/src/tests/transaction_verifier.rs` lines 100–122) covers the lock-script case (`test_not_enabled_hash_type_output_lock`) but has no corresponding test for the type-script case.

## Impact Explanation
Every CKB full node runs `NonContextualTransactionVerifier` as the cheap first-pass filter before the expensive contextual pipeline. By submitting transactions whose outputs carry a type script with `hash_type ∈ {6, 8, 10, …}`, an unprivileged sender forces each receiving node to: (1) pass the cheap non-contextual gate, (2) resolve all input/output cells (database reads), (3) build script groups and invoke `select_version`, which immediately returns `InvalidScriptHashType`, (4) reject the transaction and discard the work. The attacker pays only the cost of broadcasting; each node pays the cost of contextual resolution before rejection. This is a resource-amplification vector that can cause CKB network congestion with few costs, matching the **High** impact category.

## Likelihood Explanation
The entry path is fully open. Any peer that can submit a transaction via `send_transaction` RPC or relay it over the P2P network can trigger this. No key, stake, or special role is required. Crafting such a transaction is trivial: set `type_script.hash_type = 6` on any output with a valid lock script. The attack is repeatable and requires no victim mistake.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's `hash_type` for every output that carries one:

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

fn check_hash_type(raw: packed::Byte) -> Result<(), Error> {
    match TryInto::<ScriptHashType>::try_into(raw) {
        Ok(ht) => {
            let val: u8 = ht.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
            Ok(())
        }
        Err(_) => Err(TransactionError::InvalidScriptHashType { hash_type: raw }.into()),
    }
}
```

Also update the `NonContextualTransactionVerifier` doc comment to reflect that both lock and type script hash types are checked.

## Proof of Concept

```rust
#[test]
pub fn test_not_enabled_hash_type_output_type_script_passes_verifier() {
    // Data3 = 6: structurally valid (even), but NOT in ENABLED_SCRIPT_HASH_TYPE {0,1,2,4}
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .lock(Script::default()) // valid lock hash_type = 0 (Data)
                .type_(Some(
                    Script::default()
                        .as_builder()
                        .hash_type(ScriptHashType::Data3) // hash_type = 6
                        .build(),
                ).pack())
                .build(),
        )
        .build();

    let verifier = ScriptHashTypeVerifier::new(&transaction);

    // BUG: returns Ok(()) — the not-permitted type-script hash_type is silently accepted
    assert!(verifier.verify().is_ok());
}
```

The symmetric lock-script test (`test_not_enabled_hash_type_output_lock`, lines 100–122) correctly returns `Err(ScriptHashTypeNotPermitted)`, confirming the asymmetry is real and the fix is straightforward.