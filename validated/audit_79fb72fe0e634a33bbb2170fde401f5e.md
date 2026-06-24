Audit Report

## Title
`ScriptHashTypeVerifier::verify()` Omits Type Script Hash Type Consensus Check, Enabling Consensus Deviation — (File: `verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and checks the `ENABLED_SCRIPT_HASH_TYPE` consensus gate only for the lock script of each output. The type script hash type is never validated against this set. A transaction output carrying a consensus-disabled hash type (e.g., `Data3 = 6`) in its type script passes `NonContextualTransactionVerifier`, enters the tx-pool, and can be committed into a block, causing upgraded and non-upgraded nodes to disagree on block validity.

## Finding Description
`ENABLED_SCRIPT_HASH_TYPE` is defined as `{0, 1, 2, 4}` (Data, Type, Data1, Data2), explicitly excluding future values such as `Data3 = 6`: [1](#0-0) 

`ScriptHashTypeVerifier::verify()` iterates outputs and checks only `output.lock().hash_type()` against this set: [2](#0-1) 

There is no corresponding branch for `output.type_().to_opt()`. A type script carrying `Data3` (value `6`) passes the `TryInto::<ScriptHashType>` conversion (structurally valid — `verify_value` returns `true`) and is never tested against `ENABLED_SCRIPT_HASH_TYPE`.

The lower-level `check_data` in `util/gen-types/src/extension/check_data.rs` checks both lock and type scripts, but only for structural validity — any value that `ScriptHashType::verify_value` accepts passes, regardless of whether it is consensus-enabled: [3](#0-2) 

`ScriptHashTypeVerifier` is embedded in `NonContextualTransactionVerifier`, which is the sole non-contextual gate for both tx-pool admission and block transaction verification: [4](#0-3) 

The test suite confirms the asymmetry — only lock-script cases are covered, with no type-script counterpart: [5](#0-4) 

## Impact Explanation
`ENABLED_SCRIPT_HASH_TYPE` is the consensus gate that prevents new VM versions from being used before the activating hard fork. Bypassing it via the type script path allows a transaction to be admitted and committed that upgraded nodes (which implement the new VM) accept while non-upgraded nodes reject. This is a **Critical** consensus deviation impact: *"Vulnerabilities which could easily cause consensus deviation."*

## Likelihood Explanation
Any unprivileged user can craft a transaction with a lock script using an enabled hash type and a type script using `Data3` (value `6`, not in `ENABLED_SCRIPT_HASH_TYPE`) and submit it via the standard `send_transaction` RPC or P2P relay. The only precondition is that the node's VM already implements the corresponding version — a condition that routinely holds during the pre-fork window, since code ships before activation. No special privileges, leaked keys, or victim mistakes are required.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to validate the type script hash type for each output, mirroring the existing lock script check:

```rust
if let Some(type_script) = output.type_().to_opt() {
    if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_script.hash_type()) {
        let val: u8 = hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(
                TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into(),
            );
        }
    } else {
        return Err((TransactionError::InvalidScriptHashType {
            hash_type: type_script.hash_type(),
        })
        .into());
    }
}
```

Add corresponding unit tests for type-script cases analogous to `test_unknown_hash_type_output_lock` and `test_not_enabled_hash_type_output_lock`. [5](#0-4) 

## Proof of Concept
1. Build a transaction with one output:
   - Lock script: `hash_type = ScriptHashType::Data` (value `0`, enabled — passes check)
   - Type script: `hash_type = ScriptHashType::Data3` (value `6`, not in `ENABLED_SCRIPT_HASH_TYPE` — **not checked**)
2. Submit via `send_transaction` RPC.
3. `NonContextualTransactionVerifier::verify()` calls `ScriptHashTypeVerifier::verify()`, which iterates outputs, checks only `output.lock().hash_type()` → `0` → in set → passes. Type script hash type `6` is never inspected.
4. Transaction enters the tx-pool.
5. On a node whose VM supports version 3, script execution succeeds and the transaction is committed into a block.
6. Non-upgraded nodes reject the block, producing a consensus split.

A minimal unit test mirroring `test_not_enabled_hash_type_output_lock` but setting the type script's hash type to `ScriptHashType::Data3` and asserting `ScriptHashTypeNotPermitted` is returned will reproduce the gap: the current code returns `Ok(())` instead. [2](#0-1)

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

**File:** util/gen-types/src/extension/check_data.rs (L10-27)
```rust
impl<'r> packed::ScriptReader<'r> {
    fn check_data(&self) -> bool {
        core::ScriptHashType::verify_value(self.hash_type().into())
    }
}

impl<'r> packed::ScriptOptReader<'r> {
    fn check_data(&self) -> bool {
        self.to_opt()
            .map(|i| core::ScriptHashType::verify_value(i.hash_type().into()))
            .unwrap_or(true)
    }
}

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
