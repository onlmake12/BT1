### Title
`ScriptHashTypeVerifier` Omits Type Script Hash Type Validation for Transaction Outputs — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier`, the non-contextual gate that enforces `ENABLED_SCRIPT_HASH_TYPE` on transaction outputs, only inspects each output's **lock** script hash type. The **type** script hash type is never checked. This is the direct CKB analog of `registerWallet()` accepting any caller without verifying it is a real Safe: an entity is admitted through the early, cheap validation stage without the required type/version check.

---

### Finding Description

`NonContextualTransactionVerifier` is the first admission filter applied to every submitted transaction. It contains `ScriptHashTypeVerifier`, whose stated purpose is to reject outputs whose scripts use hash types outside the consensus-permitted set. [1](#0-0) 

Inside `ScriptHashTypeVerifier::verify()`, the loop iterates over outputs and calls `output.lock().hash_type()` — the lock script only: [2](#0-1) 

`output.type_()` is never read. A transaction output whose **type** script carries an unsupported or invalid hash type byte passes this verifier without error.

For comparison, `CellbaseVerifier` in `block_verifier.rs` explicitly forbids type scripts on cellbase outputs entirely: [3](#0-2) 

and then checks lock hash types for cellbase outputs: [4](#0-3) 

The asymmetry is clear: cellbase outputs are fully guarded; regular transaction outputs are only half-guarded.

---

### Impact Explanation

`ContextualTransactionVerifier::verify()` accepts a `skip_script_verify` flag: [5](#0-4) 

When `skip_script_verify = true`, the `ScriptVerifier` step is bypassed entirely. In that code path, the only hash-type gate that runs is the incomplete `ScriptHashTypeVerifier`. A transaction output with an unsupported type script hash type therefore clears **all** verification and is committed to the chain.

The committed cell carries a type script that no future transaction can successfully execute (the VM cannot resolve a script with an unknown hash type). The cell becomes permanently unspendable, and any CKB capacity locked in it is irrecoverable — the same "funds lost or inaccessible" impact described in the reference report.

---

### Likelihood Explanation

Low. The `skip_script_verify = true` path must be active (e.g., during IBD replay of a chain that was produced by a node that skipped verification, or any future internal caller that sets the flag), **and** a transaction with an unsupported type script hash type must reach that path. In the normal tx-pool submission flow, contextual script execution would catch the invalid hash type before commitment. The probability of both conditions coinciding is low, but the gap in the non-contextual check is unconditional and present in every code path that calls `NonContextualTransactionVerifier`.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type for each output, mirroring the lock script check:

```rust
// after checking output.lock().hash_type() ...
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

This makes the non-contextual check complete and consistent, regardless of whether script execution is later skipped.

---

### Proof of Concept

1. Craft a `TransactionView` with one output whose lock script uses a valid hash type (e.g., `Data`) and whose type script uses a hash type byte **not** in `ENABLED_SCRIPT_HASH_TYPE` (e.g., a reserved future value such as `0x04`).
2. Submit the transaction to a node running with `skip_script_verify = true` (or inject it into a block processed under that flag).
3. `NonContextualTransactionVerifier::verify()` calls `ScriptHashTypeVerifier::verify()`, which iterates outputs, checks only `output.lock().hash_type()` — passes — and returns `Ok(())`.
4. `ContextualTransactionVerifier::verify(max_cycles, true)` skips the `ScriptVerifier` step; capacity and time-relative checks pass normally.
5. The transaction is committed. The output cell now lives on-chain with a type script hash type that no VM version can resolve.
6. Any subsequent transaction attempting to consume or reference this cell as an input will fail at script execution with an unresolvable hash type, permanently locking the capacity. [6](#0-5)

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

**File:** verification/src/transaction_verifier.rs (L162-172)
```rust
    pub fn verify(&self, max_cycles: Cycle, skip_script_verify: bool) -> Result<Completed, Error> {
        self.time_relative.verify()?;
        self.capacity.verify()?;
        let cycles = if skip_script_verify {
            0
        } else {
            self.script.verify(max_cycles)?
        };
        let fee = self.fee_calculator.transaction_fee()?;
        Ok(Completed { cycles, fee })
    }
```

**File:** verification/src/transaction_verifier.rs (L787-815)
```rust
pub struct ScriptHashTypeVerifier<'a> {
    transaction: &'a TransactionView,
}

impl<'a> ScriptHashTypeVerifier<'a> {
    pub fn new(transaction: &'a TransactionView) -> Self {
        Self { transaction }
    }

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
}
```

**File:** verification/src/block_verifier.rs (L127-133)
```rust
        if cellbase_transaction
            .outputs()
            .into_iter()
            .any(|output| output.type_().is_some())
        {
            return Err((CellbaseError::InvalidTypeScript).into());
        }
```

**File:** verification/src/block_verifier.rs (L135-144)
```rust
        for output in cellbase_transaction.outputs() {
            if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
                let val: u8 = hash_type.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err((CellbaseError::InvalidOutputLock).into());
                }
            } else {
                return Err((CellbaseError::InvalidOutputLock).into());
            }
        }
```
