### Title
`ScriptHashTypeVerifier` Only Checks Lock Script Hash Type, Ignoring Type Script Hash Type — (`File: verification/src/transaction_verifier.rs`)

### Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates the hash type of each output's **lock script** against `ENABLED_SCRIPT_HASH_TYPE`, but never inspects the **type script** hash type. An unprivileged transaction sender can submit a transaction whose output carries a type script with a future/unpermitted `ScriptHashType` (e.g., `Data3` = 6), bypassing this consensus-rule gate entirely and polluting the tx-pool.

### Finding Description

`ScriptHashTypeVerifier` is the dedicated verifier that enforces the consensus rule: only hash types listed in `ENABLED_SCRIPT_HASH_TYPE` are permitted in transaction outputs. Its `verify()` method loops over outputs and calls `output.lock().hash_type()` — but never touches `output.type_()`:

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

The `ScriptHashType` enum includes future variants (`Data3` through `Data127`) whose raw values are valid (pass `check_data`) but are not yet enabled by consensus: [2](#0-1) 

The `check_data` helper only validates that the byte is a recognized enum value — it does **not** enforce the consensus-permitted set: [3](#0-2) 

So a transaction output with `type_script.hash_type = Data3` (byte value 6) will:
1. Pass `check_data` (6 is a valid even byte → recognized as `Data3`)
2. Pass `ScriptHashTypeVerifier` (only the lock script is checked)
3. Enter the tx-pool via `non_contextual_verify`
4. Fail only at script execution time inside `select_version`, which does enforce the consensus gate [4](#0-3) 

The corresponding error variant `ScriptHashTypeNotPermitted` is defined and is even classified as a **malformed transaction** (causing peer banning when received over P2P), yet the verifier that is supposed to emit it silently skips the type script field: [5](#0-4) [6](#0-5) 

The existing unit test only covers the lock script path, leaving the type script path untested: [7](#0-6) 

### Impact Explanation

Any unprivileged RPC caller (`send_transaction`) or P2P tx-relay peer can craft a transaction whose output type script carries a future, consensus-unpermitted hash type. Such transactions:

- Bypass the `ScriptHashTypeVerifier` gate and are admitted to the tx-pool, consuming pool capacity and triggering full verification work.
- Cannot be included in a valid block (script execution rejects them), so there is no consensus split or chain corruption.
- When relayed over P2P, the receiving node's `non_contextual_verify` also passes, so the invalid transaction propagates across the network before being discarded at execution time.

The net effect is **tx-pool pollution and wasted verification resources** across all reachable nodes, with no path to chain-level corruption.

### Likelihood Explanation

The attack requires only the ability to submit a transaction via JSON-RPC or P2P relay — no keys, no stake, no special role. Constructing a transaction with a type script whose `hash_type` byte is `6` (`Data3`) is trivial. The condition is permanently reachable on mainnet/testnet today because `Data3` is not yet enabled.

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type for each output:

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

1. Construct a transaction with one output whose lock script uses `hash_type: Data` (0, always enabled) and whose type script uses `hash_type: 6` (`Data3`, not yet enabled).
2. Submit via `send_transaction` RPC.
3. Observe the transaction is accepted into the tx-pool (no `ScriptHashTypeNotPermitted` error).
4. Observe the transaction is rejected only when the block assembler attempts to execute it, confirming the verifier gap.

The `check_data` path confirms byte value `6` is structurally valid: [8](#0-7) 

The `select_version` path confirms it would be rejected at execution: [9](#0-8)

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

**File:** util/gen-types/src/core.rs (L9-32)
```rust
seq!(N in 3..=127 {
    /// Specifies how the script `code_hash` is used to match the script code and how to run the code.
    /// The hash type is split into the high 7 bits and the low 1 bit,
    /// when the low 1 bit is 1, it indicates the type,
    /// when the low 1 bit is 0, it indicates the data,
    /// and then it relies on the high 7 bits to indicate
    /// that the data actually corresponds to the version.
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

**File:** util/gen-types/src/extension/check_data.rs (L10-28)
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
}
```

**File:** script/src/types.rs (L900-936)
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
```

**File:** util/types/src/core/error.rs (L213-221)
```rust
    /// The lock script hash type is not permitted by the current consensus rules.
    #[error(
        "The lock script hash type {} is not permitted by the current consensus rules.",
        hash_type
    )]
    ScriptHashTypeNotPermitted {
        /// The hash type value
        hash_type: u8,
    },
```

**File:** util/types/src/core/error.rs (L242-255)
```rust
impl TransactionError {
    /// Returns whether this transaction error indicates that the transaction is malformed.
    pub fn is_malformed_tx(&self) -> bool {
        match self {
            TransactionError::OutputsSumOverflow { .. }
            | TransactionError::DuplicateCellDeps { .. }
            | TransactionError::DuplicateHeaderDeps { .. }
            | TransactionError::Empty { .. }
            | TransactionError::InsufficientCellCapacity { .. }
            | TransactionError::InvalidSince { .. }
            | TransactionError::ExceededMaximumBlockBytes { .. }
            | TransactionError::InvalidScriptHashType { .. }
            | TransactionError::ScriptHashTypeNotPermitted { .. }
            | TransactionError::OutputsDataLengthMismatch { .. } => true,
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
