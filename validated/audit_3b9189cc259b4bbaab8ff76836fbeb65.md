Audit Report

## Title
`ScriptHashTypeVerifier::verify` Omits Type-Script `hash_type` Validation, Allowing Not-Permitted Hash Types to Bypass Non-Contextual Checks — (File: `verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` checks only the lock script's `hash_type` against `ENABLED_SCRIPT_HASH_TYPE = {0, 1, 2, 4}` and never inspects the type script's `hash_type`. A transaction output carrying `type_script.hash_type = 6` (`Data3`) passes all non-contextual checks and is only rejected during the expensive contextual pipeline, after cell resolution and script-group construction have already been performed. This creates a resource-amplification vector for network congestion.

## Finding Description
`ScriptHashTypeVerifier::verify()` at [1](#0-0)  iterates over outputs and checks only `output.lock().hash_type()` — the `output.type_().to_opt()` branch is never reached.

`ENABLED_SCRIPT_HASH_TYPE` is `{0, 1, 2, 4}` as defined at [2](#0-1) .

`ScriptHashType` is generated via `seq!(N in 3..=127 { Data~N = N << 1 })`, making `Data3 = 6`, `Data4 = 8`, etc. all valid enum variants. [3](#0-2) 

`ScriptHashType::verify_value(6)` returns `true` because `6.is_multiple_of(2)`, so `Data3` passes the earlier `check_data` gate. [4](#0-3) 

Critically, `CellOutputReader::check_data()` does check both lock and type scripts via `self.lock().check_data() && self.type_().check_data()`, but `check_data` only calls `verify_value` which accepts any even value — it does **not** enforce `ENABLED_SCRIPT_HASH_TYPE`. [5](#0-4) 

The downstream `select_version` does catch these values and returns `ScriptError::InvalidScriptHashType`, but only after cell resolution and script-group construction. [6](#0-5) 

The existing test suite covers only the lock-script case (`test_not_enabled_hash_type_output_lock`) with no corresponding type-script test. [7](#0-6) 

## Impact Explanation
Every CKB full node runs `NonContextualTransactionVerifier` as a cheap first-pass filter. By submitting transactions with `type_script.hash_type ∈ {6, 8, 10, …}`, an attacker forces each receiving node to pass the cheap non-contextual gate, resolve all input/output cells (database reads), build script groups, invoke `select_version` which immediately returns `InvalidScriptHashType`, and then discard all that work. The attacker pays only broadcast cost; each node pays contextual resolution cost. This matches the **High** impact category: **Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
The entry path is fully open. Any peer can submit a transaction via `send_transaction` RPC or relay it over the P2P network. No key, stake, or special role is required. Crafting such a transaction is trivial: set `type_script.hash_type = 6` on any output with a valid lock script. The attack is repeatable with no victim mistake required.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's `hash_type` for every output that carries one:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        check_hash_type(output.lock().hash_type())?;
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
        Err(_) => Err(TransactionError::InvalidScriptHashType { hash_type: raw }.into()),
    }
}
```

Also add a corresponding test for the type-script case symmetric to `test_not_enabled_hash_type_output_lock`. [7](#0-6) 

## Proof of Concept

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
                ).pack())
                .build(),
        )
        .build();

    let verifier = ScriptHashTypeVerifier::new(&transaction);

    // BUG: returns Ok(()) — the not-permitted type-script hash_type is silently accepted
    assert!(verifier.verify().is_ok());
}
```

The symmetric lock-script test at [7](#0-6)  correctly returns `Err(ScriptHashTypeNotPermitted)`, confirming the asymmetry is real and the fix is straightforward.

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

**File:** util/constant/src/consensus.rs (L7-11)
```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
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

**File:** util/gen-types/src/core.rs (L39-41)
```rust
    pub fn verify_value(v: u8) -> bool {
        v.is_multiple_of(2) || v == 1
    }
```

**File:** util/gen-types/src/extension/check_data.rs (L24-27)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
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
