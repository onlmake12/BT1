The code confirms the claim exactly. The `ScriptHashTypeVerifier::verify()` loop only calls `output.lock().hash_type()` and never inspects `output.type_()`. No other verification path checks type script hash types against `ENABLED_SCRIPT_HASH_TYPE`. The finding is valid.

Audit Report

## Title
`ScriptHashTypeVerifier` Omits Type Script Hash Type Consensus Check, Enabling Pre-Fork VM Bypass — (File: `verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and enforces the `ENABLED_SCRIPT_HASH_TYPE` consensus gate only on the lock script of each output. The type script hash type is never validated against this gate. A transaction carrying a structurally valid but consensus-disabled hash type (e.g., `Data3 = 6`) in a type script passes `NonContextualTransactionVerifier`, enters the tx-pool, and can be committed into a block — producing a consensus split between nodes that have already implemented the new VM version and those that have not.

## Finding Description
`ENABLED_SCRIPT_HASH_TYPE` is defined as `{0, 1, 2, 4}` (Data, Type, Data1, Data2), explicitly excluding future values such as `Data3 = 6` and `Data4 = 8`. [1](#0-0) 

`ScriptHashTypeVerifier::verify()` iterates outputs and checks only `output.lock().hash_type()`: [2](#0-1) 

There is no corresponding call to `output.type_().to_opt()` anywhere in this method. The lower-level `check_data()` on `CellOutputReader` does check both lock and type scripts, but only for structural validity (any recognized `ScriptHashType` value), not for consensus-level enablement: [3](#0-2) 

`ScriptHashTypeVerifier` is embedded in `NonContextualTransactionVerifier`, which is the gating verifier for both tx-pool admission and block transaction verification: [4](#0-3) 

The test suite confirms the asymmetry — only lock-script cases are covered, with no type-script counterparts: [5](#0-4) 

## Impact Explanation
**Critical — Consensus Deviation.** `ENABLED_SCRIPT_HASH_TYPE` is the sole consensus gate preventing new VM versions from being used before their activating hard fork. Node software routinely ships VM support before the fork. If a type script with a non-enabled hash type (e.g., `Data3`) passes `ScriptHashTypeVerifier` and the node's VM already handles that version, the transaction executes successfully and can be committed into a block. Non-upgraded nodes that do not support the VM version will reject that block, splitting the network. This matches the allowed impact: "Vulnerabilities which could easily cause consensus deviation."

## Likelihood Explanation
Any unprivileged user can craft such a transaction via the standard `send_transaction` RPC or P2P relay. The only precondition is that the node's VM implementation already supports the target hash type — a condition that routinely holds during the pre-fork window, since code ships before activation. No special privileges, leaked keys, or victim mistakes are required. The attack is repeatable and cheap.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to validate the type script hash type for each output, mirroring the existing lock script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock script check
        let lock_hash_type = TryInto::<ScriptHashType>::try_into(output.lock().hash_type())
            .map_err(|_| TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            })?;
        let val: u8 = lock_hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
        }

        // ADD: type script check
        if let Some(type_script) = output.type_().to_opt() {
            let type_hash_type = TryInto::<ScriptHashType>::try_into(type_script.hash_type())
                .map_err(|_| TransactionError::InvalidScriptHashType {
                    hash_type: type_script.hash_type(),
                })?;
            let val: u8 = type_hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(
                    TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into(),
                );
            }
        }
    }
    Ok(())
}
```

Corresponding tests for type-script cases (unknown hash type and non-enabled hash type) should be added to `verification/src/tests/transaction_verifier.rs` to mirror the existing lock-script tests. [5](#0-4) 

## Proof of Concept
1. Construct a transaction with one output:
   - Lock script: `hash_type = Data (0)` — enabled, passes check.
   - Type script: `hash_type = Data3 (6)` — not in `ENABLED_SCRIPT_HASH_TYPE`, **not checked**.
2. Submit via `send_transaction` RPC.
3. `NonContextualTransactionVerifier` runs `ScriptHashTypeVerifier::verify()`, which iterates outputs, checks only `output.lock().hash_type()` → `0` → in set → passes. Type script hash type `6` is never inspected.
4. Transaction enters the tx-pool.
5. On a node whose VM already implements version 3, script execution succeeds and the transaction is committed into a block.
6. Non-upgraded nodes reject the block → consensus split.

A minimal unit test to reproduce:
```rust
#[test]
pub fn test_not_enabled_hash_type_output_type() {
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .lock(Script::default()) // enabled hash type
                .type_(Some(
                    Script::default()
                        .as_builder()
                        .hash_type(ScriptHashType::Data3)
                        .build(),
                ).pack())
                .build(),
        )
        .build();
    let verifier = ScriptHashTypeVerifier::new(&transaction);
    // Currently passes — should return ScriptHashTypeNotPermitted
    assert!(verifier.verify().is_err());
}
```

### Citations

**File:** util/constant/src/consensus.rs (L7-12)
```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
};
```

**File:** verification/src/transaction_verifier.rs (L71-102)
```rust
pub struct NonContextualTransactionVerifier<'a> {
    pub(crate) version: VersionVerifier<'a>,
    pub(crate) size: SizeVerifier<'a>,
    pub(crate) empty: EmptyVerifier<'a>,
    pub(crate) duplicate_deps: DuplicateDepsVerifier<'a>,
    pub(crate) outputs_data_verifier: OutputsDataVerifier<'a>,
    pub(crate) script_hash_type: ScriptHashTypeVerifier<'a>,
}

impl<'a> NonContextualTransactionVerifier<'a> {
    /// Creates a new NonContextualTransactionVerifier
    pub fn new(tx: &'a TransactionView, consensus: &'a Consensus) -> Self {
        NonContextualTransactionVerifier {
            version: VersionVerifier::new(tx, consensus.tx_version()),
            size: SizeVerifier::new(tx, consensus.max_block_bytes()),
            empty: EmptyVerifier::new(tx),
            duplicate_deps: DuplicateDepsVerifier::new(tx),
            outputs_data_verifier: OutputsDataVerifier::new(tx),
            script_hash_type: ScriptHashTypeVerifier::new(tx),
        }
    }

    /// Perform context-independent verification
    pub fn verify(&self) -> Result<(), Error> {
        self.version.verify()?;
        self.size.verify()?;
        self.empty.verify()?;
        self.duplicate_deps.verify()?;
        self.outputs_data_verifier.verify()?;
        self.script_hash_type.verify()?;
        Ok(())
    }
```

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

**File:** util/gen-types/src/extension/check_data.rs (L24-27)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
```

**File:** verification/src/tests/transaction_verifier.rs (L81-122)
```rust
#[test]
pub fn test_unknown_hash_type_output_lock() {
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .lock(Script::default().as_builder().hash_type(3).build())
                .build(),
        )
        .build();
    let verifier = ScriptHashTypeVerifier::new(&transaction);

    assert_error_eq!(
        verifier.verify().unwrap_err(),
        TransactionError::InvalidScriptHashType {
            hash_type: 3.into(),
        },
    );
}

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
