### Title
`ScriptHashTypeVerifier::verify` Only Checks Output Lock Script Hash Types, Missing Output Type Script Hash Types — (`File: verification/src/transaction_verifier.rs`)

### Summary

`ScriptHashTypeVerifier::verify()` is the non-contextual gate that enforces `ENABLED_SCRIPT_HASH_TYPE` (`{0, 1, 2, 4}` — Data, Type, Data1, Data2) on transaction outputs. However, it only iterates over `output.lock().hash_type()` and never inspects `output.type_().hash_type()`. A transaction sender can therefore submit a transaction whose output carries a type script with an unenabled hash type (e.g., `Data3 = 6`) and it will pass all non-contextual checks, enter the tx-pool, and consume node resources before eventually failing at script-execution time.

### Finding Description

`ScriptHashTypeVerifier::verify()` is documented as verifying "that the ScriptHashType of transaction outputs is within the range permitted by the current consensus rules." The implementation iterates only over the lock script of each output:

```rust
// verification/src/transaction_verifier.rs  lines 796-814
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
``` [1](#0-0) 

The `output.type_()` field — an `ScriptOpt` that may contain a type script with its own `hash_type` — is never examined. The enabled set is: [2](#0-1) 

`ScriptHashType` values `6` (`Data3`), `8` (`Data4`), … up to `254` (`Data127`) are structurally valid (even numbers) and therefore pass the lower-level `check_data` gate: [3](#0-2) 

`check_data` for a `ScriptOptReader` only calls `verify_value`, which accepts any even byte or `1`: [4](#0-3) 

So a transaction whose output has `type_script.hash_type = 6` (`Data3`) passes both `check_data` and `ScriptHashTypeVerifier::verify()`, enters the tx-pool via `NonContextualTransactionVerifier`, and is only rejected later during script execution inside `TxInfo::extract_script_and_dep_index`: [5](#0-4) 

`ScriptHashTypeVerifier` is composed into `NonContextualTransactionVerifier` as the sole hash-type gate: [6](#0-5) 

### Impact Explanation

An attacker can craft transactions with valid inputs, valid lock scripts, and a fee, but with output type scripts carrying unenabled hash types (e.g., `Data3 = 6`). These transactions:

1. Pass `check_data` (structural validity only).
2. Pass `ScriptHashTypeVerifier` (lock-only check).
3. Are admitted to the tx-pool.
4. Consume tx-pool slots and CPU (non-contextual + partial contextual verification) before being evicted at script-execution time.

This enables tx-pool pollution: an attacker can displace legitimate pending transactions by flooding the pool with permanently-invalid transactions that look valid at the non-contextual layer. The node wastes memory and CPU on transactions that can never be mined.

### Likelihood Explanation

The entry path is the standard `send_transaction` RPC or P2P transaction relay — both are reachable by any unprivileged user. Constructing such a transaction requires only setting `hash_type = 6` on an output's type script, which is trivially achievable with any CKB transaction builder. No special privilege or key is required.

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type of each output, mirroring the existing lock-script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // Check lock script hash type (existing)
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

        // Check type script hash type (missing — add this)
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

### Proof of Concept

```rust
// In a test analogous to verification/src/tests/transaction_verifier.rs
#[test]
pub fn test_not_enabled_hash_type_output_type_script_passes_incorrectly() {
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .lock(Script::default()) // valid lock: Data = 0
                .type_(Some(
                    Script::default()
                        .as_builder()
                        .hash_type(ScriptHashType::Data3) // Data3 = 6, NOT in ENABLED_SCRIPT_HASH_TYPE
                        .build(),
                ))
                .build(),
        )
        .output_data(Bytes::new())
        .build();

    let verifier = ScriptHashTypeVerifier::new(&transaction);

    // BUG: this currently returns Ok(()), but should return
    // Err(ScriptHashTypeNotPermitted { hash_type: 6 })
    assert!(verifier.verify().is_ok()); // demonstrates the missing check
}
``` [7](#0-6)

### Citations

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

**File:** util/constant/src/consensus.rs (L7-12)
```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
};
```

**File:** util/gen-types/src/core.rs (L39-41)
```rust
    pub fn verify_value(v: u8) -> bool {
        v.is_multiple_of(2) || v == 1
    }
```

**File:** util/gen-types/src/extension/check_data.rs (L16-22)
```rust
impl<'r> packed::ScriptOptReader<'r> {
    fn check_data(&self) -> bool {
        self.to_opt()
            .map(|i| core::ScriptHashType::verify_value(i.hash_type().into()))
            .unwrap_or(true)
    }
}
```

**File:** script/src/types.rs (L854-860)
```rust
            hash_type => {
                return Err(ScriptError::InvalidScriptHashType(format!(
                    "The ScriptHashType/{:?} has not been activated, and is not permitted for use.",
                    hash_type
                )));
            }
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
