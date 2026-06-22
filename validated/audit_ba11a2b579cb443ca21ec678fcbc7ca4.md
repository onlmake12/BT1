### Title
Missing Type Script Hash Type Validation in `ScriptHashTypeVerifier` - (File: `verification/src/transaction_verifier.rs`)

### Summary

`ScriptHashTypeVerifier::verify()` only validates `output.lock().hash_type()` against `ENABLED_SCRIPT_HASH_TYPE`, but never checks `output.type_().hash_type()`. A transaction carrying a type script with a future/not-yet-activated `ScriptHashType` (e.g., `Data3` = 6) passes the non-contextual verifier silently, then fails only at the expensive script-execution stage with a different error. This is the direct CKB analog of the missing call-type check in `_installFallbackHandler`: a type-enum field is accepted without validation at the "admission" step, the admission appears to succeed, and the failure surfaces later (or, in a DoS scenario, the node wastes resources on contextual verification that should have been short-circuited).

---

### Finding Description

`ScriptHashTypeVerifier` is the dedicated non-contextual guard that enforces the consensus-level `ENABLED_SCRIPT_HASH_TYPE` allowlist:

```rust
// util/constant/src/consensus.rs
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // Data
    1u8, // Type
    2u8, // Data1
    4u8, // Data2
};
```

Its `verify()` loop iterates over every output but only inspects the **lock** script:

```rust
// verification/src/transaction_verifier.rs  L796-L814
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err((TransactionError::InvalidScriptHashType { hash_type: output.lock().hash_type() }).into());
        }
    }
    Ok(())
}
```

`output.type_()` is never touched. The upstream `check_data()` gate only verifies structural validity (even value or `1`):

```rust
// util/gen-types/src/extension/check_data.rs  L11-L13
impl<'r> packed::ScriptReader<'r> {
    fn check_data(&self) -> bool {
        core::ScriptHashType::verify_value(self.hash_type().into())
    }
}
```

`verify_value` accepts any even byte or `1`, so `Data3` (6), `Data4` (8), … `Data127` (254) all pass `check_data()`. The `ScriptHashType` enum itself defines all 128 variants:

```rust
// util/gen-types/src/core.rs  L9-L32
seq!(N in 3..=127 {
    pub enum ScriptHashType {
        Type = 1, Data = 0, Data1 = 2, Data2 = 4,
        #( Data~N = N << 1, )*
    }
});
```

A transaction output with `type_script.hash_type = Data3` therefore clears every non-contextual check and reaches script execution, where `select_version()` or `extract_script_and_dep_index()` finally returns `ScriptError::InvalidScriptHashType` or `ScriptError::InvalidVmVersion`.

---

### Impact Explanation

**Inconsistent validation / resource exhaustion (DoS):** `NonContextualTransactionVerifier` is the cheap, early-rejection gate. Transactions that pass it are admitted to the tx-pool and subjected to full contextual verification, which includes CKB-VM script execution — the most expensive step. An attacker who crafts outputs with a future `ScriptHashType` in the type script field can force every receiving node to run contextual verification (and potentially VM setup) for transactions that should have been rejected in O(1) at the non-contextual stage. Because the non-contextual check is also run on relayed transactions received over P2P, this is reachable by any unprivileged peer or RPC caller without any special privilege.

**Misleading error provenance:** When the transaction is eventually rejected, the error originates from `ScriptError::InvalidScriptHashType` inside the VM pipeline rather than from `TransactionError::ScriptHashTypeNotPermitted` / `TransactionError::InvalidScriptHashType` in the dedicated verifier. Tooling and integrators that inspect rejection reasons to distinguish "malformed transaction" from "script failure" will receive the wrong error class.

---

### Likelihood Explanation

Any unprivileged transaction sender or P2P relay peer can craft such a transaction. The `ScriptHashType` field is a single byte in the serialized script; setting it to `6` (`Data3`) is trivial. No key material, mining power, or privileged access is required. The attack is repeatable at negligible cost.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's hash type for every output that carries one:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock check
        check_hash_type(output.lock().hash_type())?;

        // missing type script check
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}

fn check_hash_type(raw: packed::Byte) -> Result<(), Error> {
    if let Ok(ht) = ScriptHashType::try_from(raw) {
        let val: u8 = ht.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
        }
    } else {
        return Err(TransactionError::InvalidScriptHashType { hash_type: raw }.into());
    }
    Ok(())
}
```

This mirrors the fix pattern from the original report: add the missing type check at the earliest validation point so that invalid values are rejected cheaply and with the correct error.

---

### Proof of Concept

1. Build a transaction with one output whose **type script** uses `hash_type = 6` (`Data3`):

```rust
let tx = TransactionBuilder::default()
    .output(
        CellOutput::new_builder()
            .lock(Script::default()) // valid lock (Data = 0)
            .type_(Some(
                Script::default()
                    .as_builder()
                    .hash_type(ScriptHashType::Data3) // 6 — not in ENABLED_SCRIPT_HASH_TYPE
                    .build(),
            ))
            .build(),
    )
    .output_data(Bytes::new())
    .build();
```

2. Run `ScriptHashTypeVerifier::new(&tx).verify()` — it returns `Ok(())`.

3. The existing test `test_not_enabled_hash_type_output_lock` (line 101) demonstrates that the same value in the **lock** script is correctly rejected. No equivalent test exists for the type script, confirming the gap.

4. Submit the transaction to a node via `send_transaction` RPC. It passes non-contextual validation, enters the pool, and is only rejected during contextual script execution — with `ScriptError::InvalidScriptHashType`, not `TransactionError::ScriptHashTypeNotPermitted`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** verification/src/transaction_verifier.rs (L61-102)
```rust
/// Context-independent verification checks for transaction
///
/// Basic checks that don't depend on any context
/// Contains:
/// - Check for version
/// - Check for size
/// - Check inputs and output empty
/// - Check for duplicate deps
/// - Check for whether outputs match data
/// - Check whether output lock hash type within enabled range
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

**File:** util/constant/src/consensus.rs (L1-12)
```rust
use phf::{Set, phf_set};

/// Dampening factor.
pub const TAU: u64 = 2;

/// Enabled script_hash_type
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
};
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

**File:** util/gen-types/src/core.rs (L9-42)
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
});

impl ScriptHashType {
    /// when the low 1 bit is 1, it indicates the type
    /// when the low 1 bit is 0, it indicates the data
    #[inline]
    pub fn verify_value(v: u8) -> bool {
        v.is_multiple_of(2) || v == 1
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
