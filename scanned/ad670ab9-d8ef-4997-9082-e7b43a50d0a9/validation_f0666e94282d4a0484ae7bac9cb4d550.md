Audit Report

## Title
`ScriptHashTypeVerifier::verify()` Skips Type Script Hash Type Validation, Allowing Consensus-Unpermitted Hash Types Into the Tx-Pool — (`File: verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates only the **lock script** hash type against `ENABLED_SCRIPT_HASH_TYPE`, never inspecting the **type script** hash type. A transaction whose output carries a type script with a future/unpermitted `ScriptHashType` (e.g., `Data3` = 6) passes this gate, enters the tx-pool, propagates over P2P, and is only rejected at script execution time — wasting pool capacity and verification resources across all reachable nodes.

## Finding Description

`ScriptHashTypeVerifier::verify()` loops over outputs and calls `output.lock().hash_type()` exclusively: [1](#0-0) 

The call to `output.type_()` is never made. `ENABLED_SCRIPT_HASH_TYPE` permits only `{0, 1, 2, 4}` (Data, Type, Data1, Data2): [2](#0-1) 

`ScriptHashType` includes future variants `Data3`–`Data127` (even byte values ≥ 6) generated via `seq!`: [3](#0-2) 

The `check_data` helper validates only structural correctness (even byte or 1), not the consensus-permitted set. Crucially, it checks **both** lock and type scripts for structural validity: [4](#0-3) 

So byte value `6` (`Data3`) passes `check_data` (even byte → structurally valid) and also passes `ScriptHashTypeVerifier` (only lock is checked). The transaction enters the tx-pool. It is only rejected later in `select_version` at script execution time: [5](#0-4) 

The error variant `ScriptHashTypeNotPermitted` is classified as a **malformed transaction** (triggering peer banning on P2P), yet the verifier that is supposed to emit it silently skips the type script field: [6](#0-5) 

The existing unit test covers only the lock script path, leaving the type script path entirely untested: [7](#0-6) 

## Impact Explanation

This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**. Transactions with consensus-unpermitted type script hash types bypass the `ScriptHashTypeVerifier` gate, are admitted to the tx-pool, and propagate across the P2P network before being discarded at execution time. This wastes pool capacity and triggers full verification work on every receiving node. No consensus split or chain corruption is possible, but the tx-pool pollution and cross-network propagation of invalid transactions constitutes a concrete resource-exhaustion vector.

## Likelihood Explanation

An attacker needs valid UTXOs (CKB) to submit a transaction, so the attack is not entirely costless. However, constructing a transaction with a type script whose `hash_type` byte is `6` (`Data3`) is trivial once UTXOs are available. The condition is permanently reachable on mainnet/testnet because `Data3` is not yet enabled. The attack is repeatable and requires no special privileges beyond owning some CKB.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type for each output, mirroring the existing lock script check:

```rust
// After the existing lock script check, add:
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

Also update the `ScriptHashTypeNotPermitted` error message to say "script" instead of "lock script" to reflect that both script fields are now validated. [8](#0-7) 

## Proof of Concept

1. Construct a `TransactionBuilder` with one output whose lock script uses `hash_type: Data` (0, always enabled) and whose type script uses `hash_type: 6` (`Data3`, not yet enabled).
2. Submit via `send_transaction` RPC or relay over P2P.
3. Observe the transaction is accepted into the tx-pool — `ScriptHashTypeVerifier` returns `Ok(())` because it never inspects the type script.
4. Observe the transaction is rejected only when the block assembler calls `select_version` on the type script, confirming the verifier gap.

A minimal unit test mirroring `test_not_enabled_hash_type_output_lock` but setting the type script's `hash_type` to `ScriptHashType::Data3` while leaving the lock script as `Data` would reproduce the failure: `verifier.verify()` would return `Ok(())` instead of `Err(ScriptHashTypeNotPermitted { hash_type: 6 })`. [1](#0-0)

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
