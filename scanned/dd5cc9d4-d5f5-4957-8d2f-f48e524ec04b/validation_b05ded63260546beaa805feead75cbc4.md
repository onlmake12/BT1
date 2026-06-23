### Title
`ScriptHashTypeVerifier` Checks Only Lock Script Hash Type, Silently Permitting Non-Enabled Hash Types on Type Scripts — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

The `ScriptHashTypeVerifier::verify()` method enforces the consensus rule that only hash types in `ENABLED_SCRIPT_HASH_TYPE` are permitted in transaction outputs — but it only checks the **lock script** of each output. The **type script** hash type is never validated. This is a direct analog to the external report: a limit is applied to one "direction" (lock script) but not the other (type script), allowing bypass via the unchecked path.

---

### Finding Description

`ScriptHashTypeVerifier` is documented as:

> "Verify that the ScriptHashType of transaction outputs is within the range permitted by the current consensus rules."

Its `verify()` implementation iterates over all outputs and checks only `output.lock().hash_type()`:

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
``` [1](#0-0) 

There is no corresponding check for `output.type_().to_opt().map(|t| t.hash_type())`. A transaction output whose type script carries a hash type that is structurally valid (passes `ScriptHashType::verify_value`) but not yet enabled by consensus — e.g., `Data3 = 6`, `Data4 = 8`, or any future `DataN` — will silently pass this verifier.

The lower-level `check_data` function in `util/gen-types/src/extension/check_data.rs` does check both lock and type scripts, but only for structural validity (any valid `ScriptHashType` value), not for consensus-level enablement:

```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
``` [2](#0-1) 

So a type script with `Data3` passes `check_data` (structurally valid) and also passes `ScriptHashTypeVerifier` (type script not checked), leaving the only enforcement to the script execution engine itself.

`ScriptHashTypeVerifier` is embedded in `NonContextualTransactionVerifier`, which is invoked for both tx-pool admission and block transaction verification: [3](#0-2) [4](#0-3) 

The test suite confirms the asymmetry — only lock-script cases are tested: [5](#0-4) 

---

### Impact Explanation

`ENABLED_SCRIPT_HASH_TYPE` is the consensus gate that prevents new VM versions from being used before the hard fork that activates them. If a type script can carry a non-enabled hash type without being caught by `ScriptHashTypeVerifier`, and the node's script execution engine already implements that VM version (which is common — code ships before the fork), the transaction can be admitted to the tx-pool and potentially committed into a block. This can produce a consensus split: upgraded nodes that support the new VM version accept the block; non-upgraded nodes reject it. The attacker entry path is the standard `send_transaction` RPC or P2P relay — no privilege required.

---

### Likelihood Explanation

Medium. Any unprivileged transaction sender can craft an output with a structurally valid but consensus-disabled type script hash type. The only barrier is whether the script execution engine happens to support the VM version associated with that hash type. Because node software typically implements new VM versions before the activating hard fork, this window exists in practice during the pre-fork period.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type for each output, mirroring the lock script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock script check
        let lock_hash_type = TryInto::<ScriptHashType>::try_into(output.lock().hash_type())
            .map_err(|_| TransactionError::InvalidScriptHashType { hash_type: output.lock().hash_type() })?;
        let val: u8 = lock_hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
        }

        // ADD: type script check
        if let Some(type_script) = output.type_().to_opt() {
            let type_hash_type = TryInto::<ScriptHashType>::try_into(type_script.hash_type())
                .map_err(|_| TransactionError::InvalidScriptHashType { hash_type: type_script.hash_type() })?;
            let val: u8 = type_hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        }
    }
    Ok(())
}
```

---

### Proof of Concept

Construct a transaction with:
- Output lock script: `Data` hash type (enabled, passes check)
- Output type script: `Data3` hash type (not yet enabled, **not checked**)

Submit via `send_transaction` RPC. `NonContextualTransactionVerifier` runs `ScriptHashTypeVerifier`, which iterates outputs, checks only `output.lock().hash_type()` → `Data` → passes. The type script's `Data3` hash type is never inspected. The transaction enters the tx-pool. If the node's VM supports version 3, script execution succeeds and the transaction can be committed into a block, violating the consensus rule that `Data3` is not yet permitted.

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

**File:** util/gen-types/src/extension/check_data.rs (L24-28)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
```

**File:** tx-pool/src/util.rs (L56-83)
```rust
pub(crate) fn non_contextual_verify(
    consensus: &Consensus,
    tx: &TransactionView,
) -> Result<(), Reject> {
    NonContextualTransactionVerifier::new(tx, consensus)
        .verify()
        .map_err(Reject::Verification)?;

    // The ckb consensus does not limit the size of a single transaction,
    // but if the size of the transaction is close to the limit of the block,
    // it may cause the transaction to fail to be packed
    let tx_size = tx.data().serialized_size_in_block() as u64;
    if tx_size > TRANSACTION_SIZE_LIMIT {
        return Err(Reject::ExceededTransactionSizeLimit(
            tx_size,
            TRANSACTION_SIZE_LIMIT,
        ));
    }
    // cellbase is only valid in a block, not as a loose transaction
    if tx.is_cellbase() {
        return Err(Reject::Malformed(
            "cellbase like".to_owned(),
            Default::default(),
        ));
    }

    Ok(())
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
