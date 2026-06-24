Audit Report

## Title
`ScriptHashTypeVerifier::verify()` Skips Type Script Hash Type Validation, Allowing Consensus-Unpermitted Hash Types Into the Tx-Pool — (File: verification/src/transaction_verifier.rs)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and enforces `ENABLED_SCRIPT_HASH_TYPE` only against each output's **lock script**, never inspecting the **type script**. A transaction whose output carries a type script with a future, consensus-unpermitted `ScriptHashType` (e.g., `Data3` = 6) passes non-contextual verification, enters the tx-pool, triggers full contextual verification work, and is relayed across the P2P network — all without triggering the peer-banning path that `ScriptHashTypeNotPermitted` is designed to activate.

## Finding Description

`ScriptHashTypeVerifier::verify()` (lines 796–814) loops over outputs and calls `output.lock().hash_type()` exclusively: [1](#0-0) 

The `output.type_()` field is never read. `ENABLED_SCRIPT_HASH_TYPE` permits only `{0, 1, 2, 4}` (Data, Type, Data1, Data2): [2](#0-1) 

`check_data` on `CellOutputReader` does validate both lock and type script hash bytes for structural validity (recognized enum value), but it does **not** enforce the consensus-permitted set: [3](#0-2) 

So a transaction with `type_script.hash_type = 6` (Data3):
1. Passes `check_data` — 6 is a structurally valid even byte recognized as `Data3`.
2. Passes `ScriptHashTypeVerifier` — only the lock script is checked.
3. Passes `non_contextual_verify` in the tx-pool path, which calls `NonContextualTransactionVerifier`: [4](#0-3) 

4. Is admitted to the pending pool and triggers `verify_rtx` (contextual verification), where `select_version` finally rejects it: [5](#0-4) 

The error variant `ScriptHashTypeNotPermitted` is classified as a malformed transaction (triggering peer banning on P2P receipt), yet the verifier that is supposed to emit it silently skips the type script: [6](#0-5) 

The existing unit test covers only the lock script path, leaving the type script path entirely untested: [7](#0-6) 

## Impact Explanation

This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

An attacker can spam transactions with invalid type script hash types at the cost of minimum fees. Each such transaction:
- Bypasses the non-contextual gate and occupies tx-pool capacity.
- Triggers full contextual verification (including script execution setup) before being evicted.
- Is relayed over P2P to peer nodes, where the same non-contextual gate also passes, propagating the invalid transaction network-wide and multiplying the verification burden.
- Bypasses the peer-banning mechanism that `ScriptHashTypeNotPermitted` is designed to activate, so the attacker's P2P connections are not severed.

There is no consensus split or chain corruption risk.

## Likelihood Explanation

The attack requires only the ability to submit a transaction via JSON-RPC (`send_transaction`) or P2P relay — no keys, no stake, no special role. Constructing a transaction with `type_script.hash_type = 6` is trivial. The condition is permanently reachable on mainnet/testnet because `Data3` is not in `ENABLED_SCRIPT_HASH_TYPE` and is not expected to be enabled in the near term. The attack is repeatable at scale, bounded only by the attacker's willingness to pay minimum fees.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to validate the type script hash type for each output, mirroring the existing lock script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // Existing lock script check
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

        // Add: type script check
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

A corresponding unit test should be added alongside `test_not_enabled_hash_type_output_lock` to cover the type script path.

## Proof of Concept

1. Build a `TransactionView` with one output: lock script uses `hash_type: Data` (0, always enabled); type script uses `hash_type: 6` (`Data3`, not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Call `ScriptHashTypeVerifier::new(&tx).verify()` — it returns `Ok(())`, demonstrating the gap.
3. Submit the transaction via `send_transaction` RPC on a live node — it is accepted into the tx-pool without a `ScriptHashTypeNotPermitted` error.
4. Observe the transaction is rejected only during contextual verification (`select_version` returns `InvalidScriptHashType`), confirming the verifier gap.
5. Repeat at scale to demonstrate pool capacity consumption and P2P propagation without peer banning.

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

**File:** util/gen-types/src/extension/check_data.rs (L22-26)
```rust
}

impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
```

**File:** tx-pool/src/util.rs (L56-63)
```rust
pub(crate) fn non_contextual_verify(
    consensus: &Consensus,
    tx: &TransactionView,
) -> Result<(), Reject> {
    NonContextualTransactionVerifier::new(tx, consensus)
        .verify()
        .map_err(Reject::Verification)?;

```

**File:** script/src/types.rs (L930-936)
```rust
            hash_type => {
                return Err(ScriptError::InvalidScriptHashType(format!(
                    "The ScriptHashType/{:?} has not been activated, and is not permitted for use.",
                    hash_type
                )));
            }
        }
```

**File:** util/types/src/core/error.rs (L252-255)
```rust
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
