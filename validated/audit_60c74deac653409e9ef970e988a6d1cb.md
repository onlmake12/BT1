### Title
`ScriptHashTypeVerifier::verify` Omits Type-Script `hash_type` Validation, Allowing Not-Permitted Hash Types to Bypass Non-Contextual Checks — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates the `hash_type` field of each output's **lock script** against the consensus-enforced `ENABLED_SCRIPT_HASH_TYPE` set `{0, 1, 2, 4}`. It never inspects the `hash_type` of each output's **type script**. An unprivileged transaction sender can craft an output whose type script carries a structurally valid but not-yet-enabled `hash_type` (e.g., `Data3 = 6`, `Data4 = 8`, …) and that output will silently pass the entire `NonContextualTransactionVerifier` pipeline. The transaction is only rejected later, during the more expensive script-execution stage, forcing every receiving node to perform unnecessary contextual work.

---

### Finding Description

`ScriptHashTypeVerifier::verify()` is the sole non-contextual gate that enforces the consensus rule "only hash types in `ENABLED_SCRIPT_HASH_TYPE` are permitted":

```rust
// verification/src/transaction_verifier.rs  lines 796-814
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) =
            TryInto::<ScriptHashType>::try_into(output.lock().hash_type())  // ← lock only
        {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err((TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            }).into());
        }
        // ← output.type_().to_opt() is NEVER inspected
    }
    Ok(())
}
```

The enabled set is defined as:

```rust
// util/constant/src/consensus.rs  lines 7-11
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // Data
    1u8, // Type
    2u8, // Data1
    4u8, // Data2
};
```

`ScriptHashType` is generated for every even value 0–254 plus 1 (`Type`), so `Data3 = 6`, `Data4 = 8`, … are all structurally valid (they pass `ScriptHashType::verify_value` and the molecule `check_data` gate) but are **not** in `ENABLED_SCRIPT_HASH_TYPE`. Because `ScriptHashTypeVerifier` never calls `output.type_().to_opt()`, a transaction whose output carries `type_script.hash_type = 6` passes `NonContextualTransactionVerifier` without error.

The downstream script verifier does catch it — `select_version` returns `Err(ScriptError::InvalidVmVersion(3))` for `Data3` — but only after the node has already paid the cost of contextual resolution and script-group construction.

The `NonContextualTransactionVerifier` comment itself documents the gap:

```
// Check whether output lock hash type within enabled range   ← type script omitted
```

---

### Impact Explanation

Every CKB full node runs `NonContextualTransactionVerifier` as the cheap first-pass filter before the expensive contextual pipeline (cell resolution + script execution). By submitting transactions whose outputs carry a type script with `hash_type ∈ {6, 8, 10, …}`, an unprivileged sender forces each receiving node to:

1. Accept the transaction through the cheap non-contextual gate.
2. Resolve all input/output cells (database reads).
3. Build script groups and invoke `select_version`, which immediately returns `InvalidVmVersion`.
4. Reject the transaction and discard the work.

This is a resource-amplification DoS: the attacker pays only the cost of broadcasting a transaction; each node pays the cost of contextual resolution before rejection. Because the type-script path fails before any VM bytecode is loaded, the per-transaction overhead is bounded, but the asymmetry is real and the entry path requires no privilege.

---

### Likelihood Explanation

The entry path is fully open: any peer that can submit a transaction to the RPC (`send_transaction`) or relay it over the P2P network can trigger this. No key, no stake, no special role is required. Crafting such a transaction is trivial — set `type_script.hash_type = 6` on any output.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's `hash_type` for every output that carries one:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock-script check
        check_hash_type(output.lock().hash_type())?;

        // NEW: type-script check
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}

fn check_hash_type(raw: packed::Byte) -> Result<(), Error> {
    match TryInto::<ScriptHashType>::try_into(raw) {
        Ok(ht) => {
            let val: u8 = ht.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
            Ok(())
        }
        Err(_) => Err((TransactionError::InvalidScriptHashType { hash_type: raw }).into()),
    }
}
```

---

### Proof of Concept

```rust
#[test]
pub fn test_not_enabled_hash_type_output_type_script_passes_verifier() {
    // Data3 = 6: structurally valid (even), but NOT in ENABLED_SCRIPT_HASH_TYPE {0,1,2,4}
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .lock(Script::default()) // valid lock hash_type = 0 (Data)
                .type_(Some(
                    Script::default()
                        .as_builder()
                        .hash_type(ScriptHashType::Data3) // hash_type = 6
                        .build(),
                ))
                .build(),
        )
        .build();

    let verifier = ScriptHashTypeVerifier::new(&transaction);

    // BUG: this returns Ok(()) — the not-permitted type-script hash_type is silently accepted.
    assert!(verifier.verify().is_ok(),
        "ScriptHashTypeVerifier should have rejected Data3 on the type script");
}
```

The test above passes today (verifier returns `Ok`), demonstrating the gap. The symmetric test for the lock script (`test_not_enabled_hash_type_output_lock` in the existing test suite) correctly returns `Err(ScriptHashTypeNotPermitted)`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** util/constant/src/consensus.rs (L7-11)
```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
```

**File:** util/gen-types/src/core.rs (L9-41)
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
