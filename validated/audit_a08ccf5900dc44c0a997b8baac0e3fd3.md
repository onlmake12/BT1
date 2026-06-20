### Title
Missing `ScriptHashType` Validation for Output Type Scripts Allows Permanently Unspendable Cells - (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates the `hash_type` of each output's **lock script** against `ENABLED_SCRIPT_HASH_TYPE`, but completely omits the same check for each output's **type script**. An unprivileged transaction sender can craft a transaction whose output carries a type script with a future/uninitialized `ScriptHashType` (e.g., `Data3` = 6), have it accepted on-chain, and permanently freeze the resulting cell because any subsequent spend attempt will fail script execution.

---

### Finding Description

`ScriptHashTypeVerifier::verify()` loops over outputs and checks only `output.lock().hash_type()`:

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
        } else { ... }
    }
    Ok(())
}
``` [1](#0-0) 

There is no corresponding check for `output.type_().to_opt()`. The enabled set is:

```rust
// util/constant/src/consensus.rs
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
};
``` [2](#0-1) 

`ScriptHashType` is defined with a large future-variant space via a `seq!` macro — values like `Data3 = 6`, `Data4 = 8`, … `Data127 = 254` are all structurally valid encodings (low bit = 0, high bits encode version) and pass the molecule-level `check_data()` structural check, but are **not** in `ENABLED_SCRIPT_HASH_TYPE`. [3](#0-2) 

When such a cell is later spent, `select_version()` in the script engine hits the catch-all arm and returns `ScriptError::InvalidScriptHashType`:

```rust
// script/src/types.rs  lines 930-935
hash_type => {
    return Err(ScriptError::InvalidScriptHashType(format!(
        "The ScriptHashType/{:?} has not been activated, and is not permitted for use.",
        hash_type
    )));
}
``` [4](#0-3) 

The existing unit test `test_not_enabled_hash_type_output_lock` only exercises the lock-script path; there is no analogous test for the type-script path. [5](#0-4) 

---

### Impact Explanation

A transaction output whose **type script** carries `hash_type = Data3` (byte value `6`) passes every consensus check at submission time and is committed to the chain. The cell is then permanently unspendable: every future transaction that tries to consume it must execute the type script, which immediately aborts with `InvalidScriptHashType`. Any CKB capacity locked in that cell is irrecoverably frozen. The severity is **medium** — it matches the original report's class (uninitialized/unchecked policy ID leading to undefined/broken state) and maps to the CKB bounty impact category of *permanent lock/freeze of user state*.

---

### Likelihood Explanation

The entry path is the standard `send_transaction` RPC, reachable by any unprivileged user. No special role, key, or majority hash power is required. A user could trigger this accidentally (e.g., by using a library that serialises a future `hash_type` value) or a malicious actor could deliberately target a counterparty's funds by constructing a transaction that creates such a cell on their behalf. Likelihood is **low-to-medium** in practice today (future `DataN` values are not yet in common use) but the code path is fully reachable.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's `hash_type` for every output that carries one:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock-script check
        check_hash_type(output.lock().hash_type())?;
        // missing type-script check
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}
```

where `check_hash_type` encapsulates the existing `ENABLED_SCRIPT_HASH_TYPE` lookup logic. [6](#0-5) 

---

### Proof of Concept

1. Build a transaction with one output whose type script has `hash_type = 6` (`Data3`):

```rust
let bad_type_script = Script::new_builder()
    .code_hash(Byte32::zero())
    .hash_type(ScriptHashType::Data3.into())  // byte value 6
    .build();
let output = CellOutput::new_builder()
    .capacity(capacity_bytes!(100))
    .lock(Script::default())          // valid lock (Data = 0)
    .type_(Some(bad_type_script).pack())
    .build();
let tx = TransactionBuilder::default()
    .output(output)
    .output_data(Bytes::new().pack())
    .build();
```

2. Run `ScriptHashTypeVerifier::new(&tx).verify()` — it returns `Ok(())` because only the lock script (`Data = 0`) is checked.

3. Submit via `send_transaction` RPC. The transaction is accepted and mined.

4. Attempt to spend the resulting cell. The type script execution calls `select_version()`, hits the `hash_type => Err(ScriptError::InvalidScriptHashType(...))` arm, and the spend is permanently rejected. [2](#0-1) [1](#0-0) [7](#0-6)

### Citations

**File:** verification/src/transaction_verifier.rs (L785-815)
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

**File:** util/constant/src/consensus.rs (L7-12)
```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
};
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
