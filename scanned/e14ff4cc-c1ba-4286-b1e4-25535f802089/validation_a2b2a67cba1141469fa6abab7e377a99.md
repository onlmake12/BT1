The code confirms the claim. Here is my assessment:

**Verification of key facts:**

1. `ScriptHashTypeVerifier::verify()` (lines 796–814) only calls `output.lock().hash_type()` — the type script is never consulted. [1](#0-0) 

2. `ENABLED_SCRIPT_HASH_TYPE` contains `{0, 1, 2, 4}` — `Data3` (value `3`) is absent. [2](#0-1) 

3. `check_data()` calls `ScriptHashType::verify_value()` — this only validates that the byte is a recognized enum discriminant, not that it is in the enabled set. It passes for `Data3`. [3](#0-2) 

4. The only existing test covers the lock script case; no test exists for the type script case. [4](#0-3) 

5. `NonContextualBlockTxsVerifier` propagates this gap to every block transaction. [5](#0-4) 

The gap is real and the exploit path is reachable by any unprivileged sender via `send_transaction`. The impact (consensus split between nodes that do/don't support `Data3` at the VM level) matches the Critical allowed scope.

---

Audit Report

## Title
Incomplete `ScriptHashType` Validation in `ScriptHashTypeVerifier` — Type Script `hash_type` Not Checked Against `ENABLED_SCRIPT_HASH_TYPE` - (File: `verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` is documented to enforce that all output script `hash_type` values fall within the consensus-permitted set (`ENABLED_SCRIPT_HASH_TYPE`), but its implementation only inspects each output's lock script. The type script `hash_type` is never validated. An unprivileged sender can submit a transaction whose output carries a type script with a disallowed `hash_type` (e.g., `Data3`, value `3`), bypassing the consensus gate entirely and causing divergent behavior across node versions — a consensus split.

## Finding Description
In `verification/src/transaction_verifier.rs` lines 796–814, `ScriptHashTypeVerifier::verify()` iterates over outputs and calls `output.lock().hash_type()` exclusively. The type script, accessible via `output.type_().to_opt()`, is never read:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            // only lock script checked
            ...
        }
    }
    Ok(())
}
```

`ENABLED_SCRIPT_HASH_TYPE` (`util/constant/src/consensus.rs`) contains `{0, 1, 2, 4}` (Data, Type, Data1, Data2). `Data3` (value `3`) is absent. The secondary check, `check_data()` (`util/gen-types/src/extension/check_data.rs` line 12), calls `ScriptHashType::verify_value()`, which only confirms the byte is a valid enum discriminant — it does **not** enforce the enabled set. `Data3` passes `check_data()`. There is no other pipeline stage that checks the type script `hash_type` against `ENABLED_SCRIPT_HASH_TYPE`. `NonContextualBlockTxsVerifier` (`verification/src/block_verifier.rs` lines 280–286) propagates this gap to every block transaction.

Exploit flow:
1. Attacker constructs a transaction with one output: lock script uses `ScriptHashType::Type` (enabled), type script uses `ScriptHashType::Data3` (not enabled).
2. Submits via `send_transaction` RPC.
3. `ScriptHashTypeVerifier::verify()` checks lock script → passes. Type script is never read.
4. `check_data()` validates `Data3` as a valid enum discriminant → passes.
5. Transaction enters the tx pool and block validation pipeline with a consensus-disallowed type script `hash_type`.
6. At script execution, nodes whose VM supports `Data3` semantics accept the transaction; nodes that do not reject it — producing a consensus split.

## Impact Explanation
`ENABLED_SCRIPT_HASH_TYPE` is the consensus-level gate preventing transactions that use not-yet-activated script semantics from entering the chain. Bypassing it for type scripts allows a class of transactions that some honest nodes accept and others reject. This is a **consensus deviation** — matching the Critical impact tier (15001–25000 points): "Vulnerabilities which could easily cause consensus deviation."

## Likelihood Explanation
The exploit requires only a standard `send_transaction` RPC call with a crafted transaction. No privileged access, key material, or majority hashpower is needed. `ScriptHashType::Data3` is already a named enum variant (confirmed by the test at `verification/src/tests/transaction_verifier.rs` line 108), so constructing the malformed transaction requires no reverse engineering. The attack is repeatable and cheap.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to also validate the type script `hash_type` when present:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // Existing lock script check
        let lock_hash_type = output.lock().hash_type();
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(lock_hash_type) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err(TransactionError::InvalidScriptHashType { hash_type: lock_hash_type }.into());
        }

        // Missing type script check
        if let Some(type_script) = output.type_().to_opt() {
            let type_hash_type = type_script.hash_type();
            if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_hash_type) {
                let val: u8 = hash_type.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
                }
            } else {
                return Err(TransactionError::InvalidScriptHashType { hash_type: type_hash_type }.into());
            }
        }
    }
    Ok(())
}
```

Add a corresponding unit test mirroring `test_not_enabled_hash_type_output_lock` but targeting the type script field.

## Proof of Concept
1. Build a transaction:
   - One output with lock script `hash_type = ScriptHashType::Type` (value `1`, enabled).
   - Same output with type script `hash_type = ScriptHashType::Data3` (value `3`, not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Instantiate `ScriptHashTypeVerifier::new(&transaction)` and call `.verify()`.
3. Observe: `verify()` returns `Ok(())` — the type script `hash_type` is never checked.
4. Minimal unit test (analogous to the existing `test_not_enabled_hash_type_output_lock`):

```rust
#[test]
pub fn test_not_enabled_hash_type_output_type_script() {
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .lock(Script::default().as_builder().hash_type(ScriptHashType::Type).build())
                .type_(Some(Script::default().as_builder().hash_type(ScriptHashType::Data3).build()).pack())
                .build(),
        )
        .build();
    let verifier = ScriptHashTypeVerifier::new(&transaction);
    assert_error_eq!(
        verifier.verify().unwrap_err(),
        TransactionError::ScriptHashTypeNotPermitted { hash_type: ScriptHashType::Data3.into() },
    );
}
```

This test will **fail** on the current code (returning `Ok(())` instead of an error), confirming the vulnerability.

### Citations

**File:** verification/src/transaction_verifier.rs (L796-814)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        for output in self.transaction.outputs() {
            if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
                let val: u8 = hash_type.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err(
                        TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into(),
                    );
                }
            } else {
                return Err((TransactionError::InvalidScriptHashType {
                    hash_type: output.lock().hash_type(),
                })
                .into());
            }
        }

        Ok(())
    }
```

**File:** util/constant/src/consensus.rs (L7-12)
```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
};
```

**File:** util/gen-types/src/extension/check_data.rs (L10-13)
```rust
impl<'r> packed::ScriptReader<'r> {
    fn check_data(&self) -> bool {
        core::ScriptHashType::verify_value(self.hash_type().into())
    }
```

**File:** verification/src/tests/transaction_verifier.rs (L100-122)
```rust
#[test]
pub fn test_not_enabled_hash_type_output_lock() {
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .lock(
                    Script::default()
                        .as_builder()
                        .hash_type(ScriptHashType::Data3)
                        .build(),
                )
                .build(),
        )
        .build();
    let verifier = ScriptHashTypeVerifier::new(&transaction);

    assert_error_eq!(
        verifier.verify().unwrap_err(),
        TransactionError::ScriptHashTypeNotPermitted {
            hash_type: ScriptHashType::Data3.into(),
        },
    );
}
```

**File:** verification/src/block_verifier.rs (L280-286)
```rust
    pub fn verify(&self, block: &BlockView) -> Result<Vec<()>, Error> {
        block
            .transactions()
            .iter()
            .map(|tx| NonContextualTransactionVerifier::new(tx, self.consensus).verify())
            .collect()
    }
```
