### Title
`ScriptHashTypeVerifier` Skips `type_` Script Hash Type Validation in Transaction Outputs — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` enforces the `ENABLED_SCRIPT_HASH_TYPE` allowlist only against the **lock** script of each output. The **type** script's `hash_type` field is never checked. Any transaction sender can submit an output whose type script carries a future/unapproved `ScriptHashType` (e.g., `Data3` = 6, `Data4` = 8, …) and it will silently pass the non-contextual gate, enter the tx-pool, and propagate across the network before being rejected at script-execution time.

---

### Finding Description

`ENABLED_SCRIPT_HASH_TYPE` is a compile-time allowlist of the four currently permitted hash-type byte values: [1](#0-0) 

`ScriptHashType` is defined with a macro that generates variants `Data3` through `Data127` (values 6, 8, … 254), all of which are structurally valid (they satisfy `verify_value()`) but are **not** in the allowlist: [2](#0-1) 

`ScriptHashTypeVerifier::verify()` iterates over outputs and checks only `output.lock().hash_type()`. The `output.type_()` field is never touched: [3](#0-2) 

The comment above the struct even says "Verify that the ScriptHashType of transaction **outputs** is within the range permitted by the current consensus rules," yet the implementation only covers the lock half of each output: [4](#0-3) 

This verifier is the sole non-contextual guard and is called unconditionally during `NonContextualTransactionVerifier::verify()`: [5](#0-4) 

The structural `check_data()` path (used during block/message parsing) also does not enforce the allowlist — it only calls `verify_value()`, which accepts any even byte or `1`: [6](#0-5) 

The actual rejection of an unknown hash type happens much later, inside `select_version()` at script-execution time: [7](#0-6) 

---

### Impact Explanation

A transaction sender can craft a transaction whose output carries a type script with `hash_type = Data3` (byte value `6`). The transaction:

1. Passes `check_data()` (6 is even → `verify_value()` returns `true`).
2. Passes `ScriptHashTypeVerifier` (only the lock script is checked).
3. Is admitted to the tx-pool and relayed to peers.
4. Fails only when a miner or verifier attempts to execute scripts, at `select_version()`.

Consequences:
- **Tx-pool pollution / resource exhaustion**: An attacker can flood every node's tx-pool with structurally valid but permanently-unexecutable transactions at negligible cost, because the cheap non-contextual gate does not filter them.
- **Inconsistent enforcement**: The allowlist is supposed to be the single source of truth for which VM versions are permitted. Bypassing it for type scripts means the invariant "no unapproved VM version can appear in a committed output" is not enforced at the earliest possible point.
- **Forward-compatibility risk (direct analog to the report)**: If a future hardfork activates a new `ScriptHashType` (e.g., `Data3`) by adding it to `ENABLED_SCRIPT_HASH_TYPE`, the non-contextual check will still not validate type scripts against the allowlist. New VM semantics introduced by that hash type would be accepted for type scripts without the same gate that protects lock scripts.

---

### Likelihood Explanation

Any unprivileged transaction sender reachable via the RPC (`send_transaction`) or the P2P relay path can trigger this. No special keys, hashpower, or Sybil capability is required. Crafting such a transaction requires only setting the `hash_type` byte of a type script to any even value ≥ 6.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's `hash_type` for each output, mirroring the lock-script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock-script check
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err(TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            }.into());
        }

        // NEW: type-script check
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
    }
    Ok(())
}
```

This ensures the allowlist is the single enforcement point for both script roles, consistent with the principle that new VM versions must be explicitly approved before they can appear in any output.

---

### Proof of Concept

1. Construct a transaction with one output whose lock script uses `hash_type = Data` (0) and whose type script uses `hash_type = 6` (`Data3`, not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Submit via `send_transaction` RPC (or relay over P2P).
3. Observe that `NonContextualTransactionVerifier::verify()` returns `Ok(())` — the transaction is admitted to the tx-pool.
4. Observe that script execution later returns `ScriptError::InvalidScriptHashType` — the transaction can never be committed.
5. Repeat at scale to exhaust tx-pool capacity across the network.

The existing unit tests confirm the gap: `test_not_enabled_hash_type_output_lock` tests only the lock-script path; there is no corresponding test for the type-script path. [8](#0-7)

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

**File:** util/gen-types/src/extension/check_data.rs (L10-13)
```rust
impl<'r> packed::ScriptReader<'r> {
    fn check_data(&self) -> bool {
        core::ScriptHashType::verify_value(self.hash_type().into())
    }
```

**File:** script/src/types.rs (L930-935)
```rust
            hash_type => {
                return Err(ScriptError::InvalidScriptHashType(format!(
                    "The ScriptHashType/{:?} has not been activated, and is not permitted for use.",
                    hash_type
                )));
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
