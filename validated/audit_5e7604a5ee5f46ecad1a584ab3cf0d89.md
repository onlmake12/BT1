Audit Report

## Title
Missing Type Script Hash Type Validation in `ScriptHashTypeVerifier` - (File: `verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` checks only the lock script's `hash_type` against `ENABLED_SCRIPT_HASH_TYPE`, leaving the type script's `hash_type` unchecked. A transaction output carrying a type script with `hash_type = 6` (`ScriptHashType::Data3`) passes the non-contextual verifier entirely and is rejected only inside `ContextualTransactionVerifier` after cell resolution and script-group construction, work that should have been short-circuited at the cheap admission gate.

## Finding Description

`ScriptHashTypeVerifier::verify()` at lines 796–814 of `verification/src/transaction_verifier.rs` iterates over outputs and checks only `output.lock().hash_type()`: [1](#0-0) 

`output.type_()` is never inspected. The upstream structural gate `CellOutputReader::check_data()` at line 26 of `util/gen-types/src/extension/check_data.rs` calls `ScriptHashType::verify_value()` for both lock and type scripts: [2](#0-1) 

But `verify_value()` only requires the byte to be even or equal to 1: [3](#0-2) 

Since `Data3 = 3 << 1 = 6` is even, it passes `check_data()`. The `ScriptHashType` enum generates all variants via `seq!`: [4](#0-3) 

So `TryInto::<ScriptHashType>::try_into(6u8)` succeeds and returns `ScriptHashType::Data3`. `ENABLED_SCRIPT_HASH_TYPE` is `{0, 1, 2, 4}`: [5](#0-4) 

Value `6` is not in this set, but because the type script is never checked in `ScriptHashTypeVerifier`, no error is raised. The transaction passes `non_contextual_verify()` in `tx-pool/src/util.rs`: [6](#0-5) 

It is only rejected inside `ContextualTransactionVerifier` when `SgData::new()` calls `tx_data.select_version()`, which hits the catch-all arm: [7](#0-6) 

This produces `ScriptError::InvalidScriptHashType` instead of `TransactionError::ScriptHashTypeNotPermitted`, incorrect error provenance. The test `test_not_enabled_hash_type_output_lock` confirms the lock-script path is covered: [8](#0-7) 

No equivalent test exists for the type-script path, confirming the gap.

## Impact Explanation

This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** The non-contextual verifier is the cheap O(1) admission gate. Bypassing it forces every receiving node to perform cell resolution and script-group construction before the error is surfaced, multiplying per-transaction CPU and I/O cost for the attacker's minimum-fee investment. The attack is repeatable at scale.

## Likelihood Explanation

Any unprivileged transaction sender or P2P relay peer can trigger this. Setting `hash_type = 6` in a type script is a single-byte change to a serialized script. The attacker needs only valid inputs to pass fee and resolution checks, and can repeat the attack at minimum-fee cost. No key material, mining power, or privileged access is required.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type for every output that carries one:

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
```

Add a corresponding test mirroring `test_not_enabled_hash_type_output_lock` for the type-script field.

## Proof of Concept

1. Build a transaction with a valid lock script (`Data = 0`) and a type script with `hash_type = ScriptHashType::Data3` (value `6`).
2. Call `ScriptHashTypeVerifier::new(&tx).verify()` — it returns `Ok(())` because only the lock script is checked.
3. The existing test `test_not_enabled_hash_type_output_lock` (line 101) proves the same value `6` in the lock script is correctly rejected with `TransactionError::ScriptHashTypeNotPermitted`. No equivalent test for the type script exists.
4. Submit the transaction via `send_transaction` RPC with sufficient fee. It passes `non_contextual_verify()`, passes the fee check, enters `verify_rtx()`, and is rejected only inside `ContextualTransactionVerifier` when `select_version()` hits the catch-all arm — with `ScriptError::InvalidScriptHashType` instead of `TransactionError::ScriptHashTypeNotPermitted`.

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

**File:** util/gen-types/src/extension/check_data.rs (L24-28)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
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

**File:** util/gen-types/src/core.rs (L39-41)
```rust
    pub fn verify_value(v: u8) -> bool {
        v.is_multiple_of(2) || v == 1
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

**File:** tx-pool/src/util.rs (L56-62)
```rust
pub(crate) fn non_contextual_verify(
    consensus: &Consensus,
    tx: &TransactionView,
) -> Result<(), Reject> {
    NonContextualTransactionVerifier::new(tx, consensus)
        .verify()
        .map_err(Reject::Verification)?;
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
