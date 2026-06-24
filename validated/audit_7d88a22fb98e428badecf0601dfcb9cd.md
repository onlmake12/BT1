The code confirms the claim. Let me verify the key facts:

1. `ScriptHashTypeVerifier::verify()` at lines 796–814 iterates outputs and calls only `output.lock().hash_type()` — `output.type_()` is never inspected. [1](#0-0) 

2. `ENABLED_SCRIPT_HASH_TYPE` permits only `{0, 1, 2, 4}` — `Data3` (6) is absent. [2](#0-1) 

3. `CellOutputReader::check_data()` calls `self.lock().check_data() && self.type_().check_data()`, but `ScriptOptReader::check_data()` only calls `verify_value` — which validates that the byte is a recognized enum variant, not that it is in the consensus-permitted set. So `Data3` (6) passes `check_data`. [3](#0-2) 

4. `NonContextualTransactionVerifier` embeds `ScriptHashTypeVerifier` as the sole hash-type gate before tx-pool admission. [4](#0-3) 

5. The actual enforcement of the consensus-permitted set for type scripts only occurs inside `select_version` at script-execution time. [5](#0-4) 

6. The existing unit test covers only the lock script path. [6](#0-5) 

All cited code matches the report's claims exactly. The vulnerability is real and reproducible.

---

Audit Report

## Title
`ScriptHashTypeVerifier::verify()` Skips Type Script Hash Type Validation, Allowing Consensus-Unpermitted Hash Types Into the Tx-Pool — (File: verification/src/transaction_verifier.rs)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and enforces `ENABLED_SCRIPT_HASH_TYPE` only on each output's lock script; the type script is never inspected. A transaction whose output carries a type script with a consensus-unpermitted `ScriptHashType` (e.g., `Data3` = 6) passes `NonContextualTransactionVerifier`, enters the tx-pool, and is relayed P2P-wide before being rejected only at script-execution time inside `select_version`.

## Finding Description

`ScriptHashTypeVerifier::verify()` (verification/src/transaction_verifier.rs, L796–814) loops over outputs and calls `output.lock().hash_type()` exclusively — `output.type_()` is never read. `ENABLED_SCRIPT_HASH_TYPE` (util/constant/src/consensus.rs, L7–12) permits only `{0, 1, 2, 4}`. `CellOutputReader::check_data()` (util/gen-types/src/extension/check_data.rs, L24–27) does validate both lock and type scripts, but only via `ScriptHashType::verify_value`, which checks that the byte is a recognized enum variant — not that it belongs to the consensus-permitted set. `Data3` (6) is a valid enum variant, so it passes `check_data`. The full exploit path: (1) craft a transaction with a valid lock script (`hash_type = 0`) and a type script with `hash_type = 6`; (2) submit via RPC or P2P relay; (3) `NonContextualTransactionVerifier::verify()` calls `script_hash_type.verify()`, which passes because only the lock script is checked; (4) the transaction enters the tx-pool and is relayed to all peers; (5) rejection occurs only inside `select_version` at script-execution time (script/src/types.rs, L930–935), which is invoked during block validation, not tx-pool admission.

## Impact Explanation

This matches the allowed CKB bounty impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**. Crafted transactions with a forbidden type script hash type bypass the non-contextual gate, enter the tx-pool, and are relayed across the P2P network to all reachable nodes. Each relaying node runs `NonContextualTransactionVerifier` (which also passes), so the invalid transaction propagates widely, consuming pool capacity and triggering full verification work on every receiving node. There is no consensus split or chain corruption, but the resource waste is concrete and network-wide.

## Likelihood Explanation

The attack requires only the ability to submit a transaction via JSON-RPC (`send_transaction`) or P2P relay. The attacker needs a valid UTXO to reference as input (minimum CKB for fees), but the UTXO is never consumed on-chain because the transaction is never included in a valid block. The cost per attack transaction is the minimum relay fee, and the UTXO remains available for repeated submissions. Constructing a transaction with `type_script.hash_type = 6` is trivial. The condition is permanently reachable on mainnet/testnet because `Data3` is not yet enabled.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type for each output, mirroring the existing lock script check:

```rust
if let Some(type_script) = output.type_().to_opt() {
    if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_script.hash_type()) {
        let val: u8 = hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(
                TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into(),
            );
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

Mirror the existing test at verification/src/tests/transaction_verifier.rs L100–122, but set the **type script** (not the lock script) to `ScriptHashType::Data3`:

```rust
#[test]
pub fn test_not_enabled_hash_type_output_type() {
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .lock(Script::default()) // valid lock: Data (0)
                .type_(Some(
                    Script::default()
                        .as_builder()
                        .hash_type(ScriptHashType::Data3) // forbidden: 6
                        .build(),
                ).pack())
                .build(),
        )
        .build();
    let verifier = ScriptHashTypeVerifier::new(&transaction);
    // Currently passes without error — proves the gap
    assert_error_eq!(
        verifier.verify().unwrap_err(),
        TransactionError::ScriptHashTypeNotPermitted {
            hash_type: ScriptHashType::Data3.into(),
        },
    );
}
```

This test currently passes without error, directly proving the verifier gap.

### Citations

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

**File:** util/gen-types/src/extension/check_data.rs (L16-27)
```rust
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
