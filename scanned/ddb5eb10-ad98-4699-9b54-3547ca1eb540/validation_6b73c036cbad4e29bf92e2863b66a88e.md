Audit Report

## Title
`ScriptHashTypeVerifier::verify()` Skips Type Script Hash Type Validation, Allowing Consensus-Unpermitted Hash Types Into the Tx-Pool — (`File: verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and enforces `ENABLED_SCRIPT_HASH_TYPE` only on each output's **lock script**, never on the **type script**. A transaction whose output carries a type script with a future, consensus-unpermitted `ScriptHashType` (e.g., `Data3` = 6) passes `non_contextual_verify` and is admitted to the tx-pool, then propagated over P2P, before being rejected only at script-execution time. This enables low-cost tx-pool pollution and network-wide relay of invalid transactions.

## Finding Description

`ScriptHashTypeVerifier::verify()` at [1](#0-0)  loops over outputs and calls `output.lock().hash_type()` exclusively — `output.type_()` is never inspected.

`ENABLED_SCRIPT_HASH_TYPE` permits only `{0, 1, 2, 4}` (Data, Type, Data1, Data2): [2](#0-1) 

`check_data` in `CellOutputReader` validates both lock and type scripts for structural validity (recognized enum values), but does **not** enforce the consensus-permitted set: [3](#0-2) 

So a transaction output with `type_script.hash_type = 6` (`Data3`):
1. Passes `check_data` — byte 6 is a valid even value recognized as `Data3`
2. Passes `ScriptHashTypeVerifier` — only the lock script is checked
3. Enters the tx-pool via `NonContextualTransactionVerifier` (which embeds `ScriptHashTypeVerifier`): [4](#0-3) 
4. Is rejected only inside `select_version` at script-execution time, which does enforce the consensus gate: [5](#0-4) 

The existing unit test covers only the lock script path, leaving the type script path entirely untested: [6](#0-5) 

`ScriptHashTypeNotPermitted` is classified as a malformed transaction (triggering peer banning on P2P receipt), yet the verifier that is supposed to emit it silently skips the type script field: [7](#0-6) 

## Impact Explanation

This matches the allowed CKB bounty impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**. Crafted transactions with a forbidden type script hash type bypass the gate, enter the tx-pool, and are relayed across the P2P network to all reachable nodes before being discarded at execution time. Each relaying node runs `non_contextual_verify` (which also passes), so the invalid transaction propagates widely, consuming pool capacity and triggering full verification work on every node that receives it. There is no consensus split or chain corruption, but the resource waste is concrete and network-wide.

## Likelihood Explanation

The attack requires only the ability to submit a transaction via JSON-RPC (`send_transaction`) or P2P relay. The attacker needs a valid UTXO to pay fees (minimum CKB), but the cost per attack transaction is the minimum relay fee. Because the transaction is rejected from the pool after execution fails, the UTXO is freed and the attack can be repeated. Constructing a transaction with `type_script.hash_type = 6` is trivial. The condition is permanently reachable on mainnet/testnet because `Data3` is not yet enabled.

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

Add a corresponding unit test analogous to `test_not_enabled_hash_type_output_lock` that sets the type script's `hash_type` to `Data3` and asserts `ScriptHashTypeNotPermitted` is returned.

## Proof of Concept

1. Construct a `TransactionBuilder` with one output whose lock script uses `hash_type: Data` (0, always enabled) and whose type script uses `hash_type: 6` (`Data3`, not yet enabled).
2. Submit via `send_transaction` RPC or relay over P2P.
3. Observe the transaction is accepted into the tx-pool — no `ScriptHashTypeNotPermitted` error is returned.
4. Observe the transaction is rejected only when script execution runs `select_version`, confirming the verifier gap.

The minimal unit-test form mirrors the existing test at [6](#0-5)  but sets the type script (not the lock script) to `ScriptHashType::Data3` and asserts `ScriptHashTypeNotPermitted` — which currently passes without error, proving the gap.

### Citations

**File:** verification/src/transaction_verifier.rs (L71-78)
```rust
pub struct NonContextualTransactionVerifier<'a> {
    pub(crate) version: VersionVerifier<'a>,
    pub(crate) size: SizeVerifier<'a>,
    pub(crate) empty: EmptyVerifier<'a>,
    pub(crate) duplicate_deps: DuplicateDepsVerifier<'a>,
    pub(crate) outputs_data_verifier: OutputsDataVerifier<'a>,
    pub(crate) script_hash_type: ScriptHashTypeVerifier<'a>,
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

**File:** util/constant/src/consensus.rs (L7-12)
```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
};
```

**File:** util/gen-types/src/extension/check_data.rs (L22-25)
```rust
}

impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
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
