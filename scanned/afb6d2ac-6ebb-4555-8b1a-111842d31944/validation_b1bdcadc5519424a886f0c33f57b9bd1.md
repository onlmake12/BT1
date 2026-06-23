### Title
`ScriptHashTypeVerifier` Applies `hash_type` Validity Check Only to Lock Scripts, Skipping Type Scripts — (`verification/src/transaction_verifier.rs`)

---

### Summary

The `ScriptHashTypeVerifier` is documented as verifying that the `ScriptHashType` of **transaction outputs** is within the range permitted by current consensus rules. However, its implementation only inspects `output.lock().hash_type()` and completely ignores `output.type_().to_opt().map(|t| t.hash_type())`. This is a direct granularity mismatch: the property (hash_type validity) should be enforced per-script (both lock and type), but is only enforced per-lock-script. This is structurally identical to the reported bug where `isFreezable` was stored per-selector instead of per-facet.

---

### Finding Description

In `verification/src/transaction_verifier.rs`, the `ScriptHashTypeVerifier::verify()` method iterates over all transaction outputs and checks only the lock script's `hash_type`:

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
            }).into());
        }
    }
    Ok(())
}
``` [1](#0-0) 

There is no corresponding check for `output.type_().to_opt()`. A transaction output's type script can carry any `hash_type` byte value — including values that are not yet permitted by the current consensus (e.g., `Data2 = 4` before the CKB2023 hardfork activates) — and this verifier will silently pass it.

The `ScriptHashTypeVerifier` is invoked as part of `NonContextualTransactionVerifier`, which is the cheap, early-rejection gate run before expensive contextual script execution. Its purpose is to reject structurally invalid transactions before they consume significant node resources. [2](#0-1) 

The `ScriptGroup` construction in `TxData::new()` groups cells by script hash and will eventually call `select_version()` on the type script, which does check the hash_type — but only during full contextual execution:

```rust
ScriptHashType::Data2 => {
    if is_vm_version_2_and_syscalls_3_enabled {
        Ok(ScriptVersion::V2)
    } else {
        Err(ScriptError::InvalidVmVersion(2))
    }
}
``` [3](#0-2) 

This means the type script hash_type is only caught at the expensive contextual stage, not the cheap non-contextual stage.

---

### Impact Explanation

**Resource exhaustion / verification pipeline bypass**: An unprivileged transaction sender can craft a transaction whose output has a valid lock script but a type script with an unpermitted `hash_type` (e.g., `Data2` before CKB2023 hardfork). This transaction passes `NonContextualTransactionVerifier` (including `ScriptHashTypeVerifier`) and is admitted into the full contextual verification pipeline, consuming CPU cycles for script group construction and VM setup before being rejected by `select_version()` with `ScriptError::InvalidVmVersion`.

**Error classification inconsistency**: When the lock script has an unpermitted hash_type, the rejection is `TransactionError::ScriptHashTypeNotPermitted`, which `is_malformed_tx()` classifies as malformed. When the type script has an unpermitted hash_type, the rejection is `ScriptError::InvalidVmVersion` from the script engine. Both are classified as malformed by `is_malformed_tx()`, but the error path differs, and the rejection happens at a different, more expensive stage. [4](#0-3) 

---

### Likelihood Explanation

Any unprivileged RPC caller or P2P transaction relayer can submit a transaction with an output whose type script carries an unpermitted `hash_type`. No special privilege, key material, or majority hashpower is required. The attack is trivially constructable by setting `hash_type` to a value not in `ENABLED_SCRIPT_HASH_TYPE` on a type script field. The node will perform full contextual verification (including script group construction) before rejecting it.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also check the type script's `hash_type` for each output, mirroring the lock script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // Check lock script hash_type (existing)
        check_hash_type(output.lock().hash_type())?;
        // Check type script hash_type (missing)
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}
```

This establishes a 1:1 enforcement of the hash_type validity property across all scripts in an output, not just lock scripts — directly analogous to the fix in the reported issue (moving `isFreezable` from per-selector to per-facet).

---

### Proof of Concept

1. Construct a transaction output with:
   - A valid lock script using `hash_type = Data` (permitted)
   - A type script using `hash_type = 4` (`Data2`) on a node running before the CKB2023 hardfork
2. Submit via `send_transaction` RPC or P2P relay
3. Observe: `ScriptHashTypeVerifier` passes (only checks lock script)
4. Observe: Transaction proceeds to full contextual verification
5. Observe: Rejection occurs at `select_version()` with `ScriptError::InvalidVmVersion(2)` instead of the expected early `TransactionError::ScriptHashTypeNotPermitted`

The attacker can repeat this at high frequency to force nodes to perform expensive contextual verification on structurally invalid transactions, consuming CPU in the verification worker pool. [1](#0-0) [5](#0-4)

### Citations

**File:** verification/src/transaction_verifier.rs (L785-795)
```rust
// Verify that the ScriptHashType of transaction outputs
// is within the range permitted by the current consensus rules.
pub struct ScriptHashTypeVerifier<'a> {
    transaction: &'a TransactionView,
}

impl<'a> ScriptHashTypeVerifier<'a> {
    pub fn new(transaction: &'a TransactionView) -> Self {
        Self { transaction }
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

**File:** script/src/types.rs (L900-937)
```rust
    pub fn select_version(&self, script: &Script) -> Result<ScriptVersion, ScriptError> {
        let is_vm_version_2_and_syscalls_3_enabled = self.is_vm_version_2_and_syscalls_3_enabled();
        let is_vm_version_1_and_syscalls_2_enabled = self.is_vm_version_1_and_syscalls_2_enabled();
        let script_hash_type = ScriptHashType::try_from(script.hash_type())
            .map_err(|err| ScriptError::InvalidScriptHashType(err.to_string()))?;
        match script_hash_type {
            ScriptHashType::Data => Ok(ScriptVersion::V0),
            ScriptHashType::Data1 => {
                if is_vm_version_1_and_syscalls_2_enabled {
                    Ok(ScriptVersion::V1)
                } else {
                    Err(ScriptError::InvalidVmVersion(1))
                }
            }
            ScriptHashType::Data2 => {
                if is_vm_version_2_and_syscalls_3_enabled {
                    Ok(ScriptVersion::V2)
                } else {
                    Err(ScriptError::InvalidVmVersion(2))
                }
            }
            ScriptHashType::Type => {
                if is_vm_version_2_and_syscalls_3_enabled {
                    Ok(ScriptVersion::V2)
                } else if is_vm_version_1_and_syscalls_2_enabled {
                    Ok(ScriptVersion::V1)
                } else {
                    Ok(ScriptVersion::V0)
                }
            }
            hash_type => {
                return Err(ScriptError::InvalidScriptHashType(format!(
                    "The ScriptHashType/{:?} has not been activated, and is not permitted for use.",
                    hash_type
                )));
            }
        }
    }
```

**File:** util/types/src/core/tx_pool.rs (L69-85)
```rust
fn is_malformed_from_verification(error: &Error) -> bool {
    match error.kind() {
        ErrorKind::Transaction => error
            .downcast_ref::<TransactionError>()
            .expect("error kind checked")
            .is_malformed_tx(),
        ErrorKind::Script => !format!("{}", error).contains(ARGV_TOO_LONG_TEXT),
        ErrorKind::Internal => {
            error
                .downcast_ref::<InternalError>()
                .expect("error kind checked")
                .kind()
                == InternalErrorKind::CapacityOverflow
        }
        _ => false,
    }
}
```
