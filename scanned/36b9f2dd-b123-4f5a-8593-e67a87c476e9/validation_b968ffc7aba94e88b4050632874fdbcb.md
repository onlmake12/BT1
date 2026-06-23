### Title
`ScriptHashTypeVerifier` Validates Only Lock Script Hash Type, Skipping Type Script Hash Type Enforcement — (`File: verification/src/transaction_verifier.rs`)

### Summary

`ScriptHashTypeVerifier::verify()` is documented to enforce that "the ScriptHashType of transaction outputs is within the range permitted by the current consensus rules," but its implementation only inspects `output.lock().hash_type()`. It never checks `output.type_().hash_type()`. An unprivileged transaction sender can submit a transaction whose output carries a type script with a hash type not yet enabled by consensus (e.g., `Data3`, `Data4`, …), and the verifier will silently pass it. This is the direct CKB analog of the SecondSwap bug: a type-discriminated validation is applied to the wrong variant, leaving the other variant's constraint unenforced.

### Finding Description

In `verification/src/transaction_verifier.rs`, `ScriptHashTypeVerifier::verify()` iterates over every output and checks only the **lock** script's hash type:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
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
    }
    Ok(())
}
```

`output.type_()` is never read. A cell output has two scripts — a mandatory lock script and an optional type script — and both can carry a `hash_type` field. The `ScriptHashType` enum includes future variants (`Data3`, `Data4`, … `Data127`) whose numeric values are valid molecule bytes but are not yet activated by consensus (`ENABLED_SCRIPT_HASH_TYPE`). The verifier enforces the enabled-range constraint only for lock scripts; type scripts are exempt.

`NonContextualTransactionVerifier` calls this verifier as one of its mandatory checks before a transaction is admitted to the tx pool or included in a block:

```rust
pub fn verify(&self) -> Result<(), Error> {
    self.version.verify()?;
    self.size.verify()?;
    self.empty.verify()?;
    self.duplicate_deps.verify()?;
    self.outputs_data_verifier.verify()?;
    self.script_hash_type.verify()?;   // ← only checks lock hash_type
    Ok(())
}
```

The lower-level `check_data()` path (used in the relayer/sync layer) does visit both lock and type scripts, but it only tests whether the raw byte is a *valid enum discriminant* — it does not enforce the *consensus-enabled* subset. So `check_data()` does not compensate for the missing check.

### Impact Explanation

A transaction sender can craft a transaction whose output has a type script with `hash_type = Data3` (value `6`) or any other future-but-not-yet-enabled variant. `ScriptHashTypeVerifier` passes it. The transaction enters the tx pool. Depending on whether the block verifier applies the same incomplete check, two outcomes are possible:

1. **Tx-pool inconsistency / resource waste**: Nodes that perform additional checks at block-inclusion time will reject the transaction then, but it will have consumed tx-pool slots and relay bandwidth.
2. **Consensus split (if the block verifier has the same gap)**: A miner that includes such a transaction produces a block that nodes with stricter validation reject, causing a chain fork. This is a consensus-safety violation reachable by any unprivileged transaction sender.

### Likelihood Explanation

Any RPC caller or P2P tx-pool submitter can craft such a transaction with zero privilege. The `ScriptHashType` enum already defines the future variants (`Data3`…`Data127`) in the codebase, so the attacker only needs to set the type script's `hash_type` byte to `6` (or any even value ≥ 6 that is a valid enum repr but outside `ENABLED_SCRIPT_HASH_TYPE`). No key material, mining power, or social engineering is required.

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's hash type for every output, mirroring the existing lock-script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // Check lock script hash type (existing)
        let lock_ht = output.lock().hash_type();
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(lock_ht) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err((TransactionError::InvalidScriptHashType { hash_type: lock_ht }).into());
        }

        // Check type script hash type (missing today)
        if let Some(type_script) = output.type_().to_opt() {
            let type_ht = type_script.hash_type();
            if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_ht) {
                let val: u8 = hash_type.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
                }
            } else {
                return Err((TransactionError::InvalidScriptHashType { hash_type: type_ht }).into());
            }
        }
    }
    Ok(())
}
```

### Proof of Concept

1. Build a transaction with one output whose lock script uses `hash_type = Type` (valid, enabled) and whose type script uses `hash_type = 6` (`Data3`, valid enum repr, not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Submit via `send_transaction` RPC.
3. `NonContextualTransactionVerifier::verify()` calls `ScriptHashTypeVerifier::verify()`, which only checks the lock script hash type (`Type` = 1, enabled → passes) and never reads the type script hash type.
4. The transaction is accepted into the tx pool despite carrying a consensus-forbidden type script hash type.

**Root cause code:** [1](#0-0) 

**Verifier is called unconditionally here:** [2](#0-1) 

**`ScriptHashType` future variants exist in the enum:** [3](#0-2) 

**`check_data()` checks both scripts but only for valid discriminant, not enabled range:** [4](#0-3)

### Citations

**File:** verification/src/transaction_verifier.rs (L94-101)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        self.version.verify()?;
        self.size.verify()?;
        self.empty.verify()?;
        self.duplicate_deps.verify()?;
        self.outputs_data_verifier.verify()?;
        self.script_hash_type.verify()?;
        Ok(())
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

**File:** util/gen-types/src/core.rs (L16-32)
```rust
     #[derive(Default, Clone, Copy, PartialEq, Eq, Debug, Hash, FromRepr)]
     #[repr(u8)]
    pub enum ScriptHashType {
        /// Type "type" matches script code via cell type script hash.
        Type = 1,
        /// Type "data" matches script code via cell data hash, and run the script code in v0 CKB VM.
        #[default]
        Data = 0,
        /// Type "data1" matches script code via cell data hash, and run the script code in v1 CKB VM.
        Data1 = 2,
        /// Type "data2" matches script code via cell data hash, and run the script code in v2 CKB VM.
        Data2 = 4,
        #(
            #[doc = concat!("Type \"data", stringify!(N), "\" matches script code via cell data hash, and runs the script code in v", stringify!(N), " CKB VM.")]
            Data~N = N << 1,
        )*
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
