### Title
Incomplete `ScriptHashType` Filtering in `ScriptHashTypeVerifier` — Output Type Scripts Not Validated Against Enabled Set - (File: verification/src/transaction_verifier.rs)

---

### Summary

`ScriptHashTypeVerifier::verify()` checks only the `hash_type` of output **lock** scripts against the `ENABLED_SCRIPT_HASH_TYPE` allowlist. Output **type** scripts are never checked. A transaction submitter can craft a transaction whose output carries a type script with an unactivated `ScriptHashType` (e.g., `Data3` = 6) and it will pass this pre-execution gate, bypassing the intended consensus-level type filter.

---

### Finding Description

`util/constant/src/consensus.rs` defines the permitted set:

```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
};
``` [1](#0-0) 

The `ScriptHashType` enum, however, is defined for all even values 0–254 and value 1 (via `seq!` macro expansion), meaning `Data3` (6), `Data4` (8), … `Data127` (254) are all valid Rust enum variants: [2](#0-1) 

The `ScriptHashTypeVerifier::verify()` method in `verification/src/transaction_verifier.rs` iterates over outputs and checks **only** `output.lock().hash_type()`:

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
        } else { ... }
    }
    Ok(())
}
``` [3](#0-2) 

The verifier **never calls `output.type_()`**. A transaction output with a type script using `ScriptHashType::Data3` (value 6) passes this verifier without error. The code comment directly above the struct states its intent is to verify that `ScriptHashType` of transaction outputs is within the range permitted by consensus rules — but it only enforces this for lock scripts. [4](#0-3) 

The downstream guard in `extract_script_and_dep_index` does catch unactivated hash types at script execution time:

```rust
hash_type => {
    return Err(ScriptError::InvalidScriptHashType(format!(
        "The ScriptHashType/{:?} has not been activated, and is not permitted for use.",
        hash_type
    )));
}
``` [5](#0-4) 

But this guard fires only during full VM execution — after the transaction has already been admitted through the pre-execution pipeline.

---

### Impact Explanation

The `ScriptHashTypeVerifier` is a lightweight pre-execution gate intended to reject structurally invalid transactions before expensive script execution. Because it omits the type script check, a transaction submitter can inject outputs with unactivated type script hash types that:

1. Pass `ScriptHashTypeVerifier` (the pre-execution gate).
2. Are admitted into the tx-pool pending full verification.
3. Fail only at full script execution (`extract_script_and_dep_index` / `select_version`).

This creates a tx-pool pollution / resource exhaustion vector: an attacker can continuously submit cheap, structurally valid transactions (valid lock scripts, valid capacity, valid molecule encoding) that carry type scripts with unactivated hash types. Each such transaction passes the fast-path check and occupies tx-pool slots until evicted by the script verifier, enabling sustained tx-pool DoS without mining power.

---

### Likelihood Explanation

High. Any unprivileged tx-pool submitter or RPC caller (`send_transaction`) can craft such a transaction with zero mining cost. The `ScriptHashType` enum exposes `Data3` through `Data127` as valid Rust values. Constructing a `packed::Script` with `hash_type = 6` requires only a single byte change. No privileged access, key material, or majority hashpower is needed.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the `hash_type` of each output's optional type script against `ENABLED_SCRIPT_HASH_TYPE`:

```rust
// After checking output.lock().hash_type() ...
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
```

This mirrors the existing lock-script check and closes the gap at the pre-execution gate, consistent with the verifier's stated purpose.

---

### Proof of Concept

```rust
// Craft a transaction whose output has a type script with Data3 (hash_type = 6)
let malicious_type_script = Script::default()
    .as_builder()
    .hash_type(ScriptHashType::Data3)  // value = 6, not in ENABLED_SCRIPT_HASH_TYPE
    .build();

let output = CellOutput::new_builder()
    .lock(Script::default())           // valid lock script (Data = 0)
    .type_(Some(malicious_type_script))
    .capacity(capacity_bytes!(100))
    .build();

let transaction = TransactionBuilder::default()
    .output(output)
    .output_data(Bytes::default())
    .build();

// ScriptHashTypeVerifier passes — only lock hash_type is checked
let verifier = ScriptHashTypeVerifier::new(&transaction);
assert!(verifier.verify().is_ok()); // ← passes, gap confirmed

// Full script execution would later reject it, but tx-pool slot is already consumed
```

The existing test `test_not_enabled_hash_type_output_lock` in `verification/src/tests/transaction_verifier.rs` confirms the lock-script path is tested, but no analogous test exists for type scripts — confirming the gap is untested and unguarded. [6](#0-5)

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

**File:** script/src/types.rs (L854-860)
```rust
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
