Audit Report

## Title
`ScriptHashTypeVerifier::verify()` Skips Type Script Hash Type Validation, Allowing Consensus-Unpermitted Hash Types Into the Tx-Pool — (`File: verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates only the **lock script** hash type against `ENABLED_SCRIPT_HASH_TYPE`, never inspecting the **type script** hash type. An unprivileged attacker can craft a transaction whose output type script carries a future, consensus-unpermitted `ScriptHashType` (e.g., `Data3` = 6), bypass this verifier entirely, and pollute the tx-pool across all reachable nodes.

## Finding Description

`ScriptHashTypeVerifier::verify()` at [1](#0-0)  loops over outputs and calls `output.lock().hash_type()` exclusively — `output.type_()` is never inspected.

`ENABLED_SCRIPT_HASH_TYPE` at [2](#0-1)  contains only `{0, 1, 2, 4}` (Data, Type, Data1, Data2). `Data3` = 6 is absent.

`CellOutputReader::check_data()` at [3](#0-2)  validates both lock and type scripts structurally via `ScriptHashType::verify_value()`, which only confirms the byte is a recognized enum variant — it does **not** enforce the consensus-permitted set. So `Data3` (6) passes `check_data`.

The exploit path is:
1. Construct a transaction with a lock script using `hash_type: Data` (0, always enabled) and a type script using `hash_type: 6` (`Data3`).
2. Submit via `send_transaction` RPC or P2P relay.
3. `check_data` passes (6 is a valid even byte, recognized as `Data3`).
4. `ScriptHashTypeVerifier::verify()` passes (only the lock script is checked).
5. Transaction enters the tx-pool.
6. Transaction is rejected only at script execution time via `select_version`'s catch-all arm at [4](#0-3) , which returns `ScriptError::InvalidScriptHashType` for any unactivated hash type.

The existing unit test at [5](#0-4)  covers only the lock script path; the type script path is entirely untested.

## Impact Explanation

This maps to the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** An attacker with any UTXOs can submit transactions with invalid type script hash types that are accepted into the tx-pool and relayed across the P2P network. Each receiving node's `non_contextual_verify` also passes (same verifier gap), so the invalid transaction propagates network-wide before being discarded at execution time. This wastes tx-pool capacity and full-verification CPU across all reachable nodes. There is no consensus split or chain corruption.

## Likelihood Explanation

The attack requires only the ability to submit a transaction via JSON-RPC or P2P relay — no keys beyond owning a UTXO, no stake, no special role. Constructing a transaction with `type_script.hash_type = 6` is trivial. The condition is permanently reachable on mainnet/testnet because `Data3` is not yet activated. The attacker can reuse different UTXOs to submit multiple such transactions, each consuming tx-pool capacity.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` at [1](#0-0)  to also validate the type script hash type for each output, mirroring the existing lock script check:

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

1. Build a `CellOutput` with `lock.hash_type = 0` (Data) and `type_.hash_type = 6` (Data3).
2. Wrap it in a `TransactionBuilder` and call `ScriptHashTypeVerifier::new(&tx).verify()`.
3. Observe `Ok(())` is returned — no error — confirming the verifier gap.
4. Submit the transaction via `send_transaction` RPC and observe it is accepted into the tx-pool.
5. Observe the transaction is rejected only when block assembly triggers `select_version` at [4](#0-3) , confirming the verifier gap allows pool admission.

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
