### Title
`ScriptHashTypeVerifier` Only Checks Lock Script Hash Type, Silently Ignoring Type Script — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` is intended to enforce that only consensus-permitted `ScriptHashType` values appear in transaction outputs. However, it only inspects the **lock script** of each output and completely ignores the **type script**. A transaction output carrying an unpermitted `hash_type` in its type script passes this check unchallenged, making the protection partially ineffective — a direct structural analog to the Vader council-veto bug where the wrong data field was inspected.

---

### Finding Description

In `verification/src/transaction_verifier.rs`, `ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates the `hash_type` of each output's **lock script** against `ENABLED_SCRIPT_HASH_TYPE`: [1](#0-0) 

The loop body calls `output.lock().hash_type()` exclusively. It never calls `output.type_().to_opt()` to retrieve and validate the type script's `hash_type`. A CellOutput can carry both a lock script and an optional type script, each with its own independent `hash_type` field.

The analogous check in `CellbaseVerifier` for the cellbase witness also only validates the lock script hash type: [2](#0-1) 

And the cellbase output lock check: [3](#0-2) 

Neither location checks the type script's `hash_type`.

The `ENABLED_SCRIPT_HASH_TYPE` constant is the consensus gate that controls which hash type values are permitted at a given protocol epoch. Its purpose is to prevent use of hash types that are not yet activated (e.g., `Data2 = 4` before the relevant hardfork). Because the type script path is never validated, a transaction output with `hash_type = 4` (or any future unpermitted value) in its **type script** will pass `NonContextualTransactionVerifier` entirely: [4](#0-3) 

---

### Impact Explanation

An unprivileged transaction sender can submit a transaction whose output type script carries an unpermitted `ScriptHashType` value. This transaction passes non-contextual verification and enters the tx-pool. If the type script is later executed (e.g., during block validation via `ContextualTransactionVerifier`), nodes running different software versions or hardfork states may handle the unknown hash type differently, creating a **consensus split**. Even before execution, accepting such transactions into the mempool and relaying them violates the protocol's stated invariant that only permitted hash types circulate on the network.

---

### Likelihood Explanation

Any transaction sender can craft such a transaction with zero special privilege. The non-contextual verifier is the first and cheapest gate; bypassing it means the malformed transaction propagates through the relay layer before any deeper check occurs. The attack requires only knowledge of the CKB transaction format, which is fully public.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the `hash_type` of each output's type script when one is present:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // Check lock script hash_type
        Self::check_hash_type(output.lock().hash_type())?;

        // Also check type script hash_type if present
        if let Some(type_script) = output.type_().to_opt() {
            Self::check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}
```

Apply the same fix to `CellbaseVerifier` for any cellbase output type scripts (currently required to be absent, but the check should be defensive). Add tests that explicitly verify a transaction with an unpermitted `hash_type` in a type script is rejected.

---

### Proof of Concept

1. Construct a `TransactionView` with one output whose **lock script** uses `ScriptHashType::Data` (permitted) and whose **type script** uses `hash_type = 4` (`Data2`, unpermitted before the relevant hardfork).
2. Run `NonContextualTransactionVerifier::new(&tx, &consensus).verify()`.
3. Observe: verification returns `Ok(())` — the unpermitted type script hash type is never checked.
4. The transaction is accepted into the tx-pool and relayed to peers, violating the consensus invariant enforced by `ENABLED_SCRIPT_HASH_TYPE`. [5](#0-4)

### Citations

**File:** verification/src/transaction_verifier.rs (L94-102)
```rust
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

**File:** verification/src/block_verifier.rs (L106-124)
```rust
        if cellbase_transaction
            .witnesses()
            .get(0)
            .and_then(|witness| {
                CellbaseWitness::from_slice(&witness.raw_data())
                    .ok()
                    .and_then(|cellbase_witness| {
                        ScriptHashType::try_from(cellbase_witness.lock().hash_type())
                            .ok()
                            .and_then(|hash_type| {
                                let val: u8 = hash_type.into();
                                ENABLED_SCRIPT_HASH_TYPE.contains(&val).then_some(())
                            })
                    })
            })
            .is_none()
        {
            return Err((CellbaseError::InvalidWitness).into());
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
