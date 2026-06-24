Audit Report

## Title
Incomplete `ScriptHashType` Validation in `ScriptHashTypeVerifier` — Output Type Scripts Not Checked Against `ENABLED_SCRIPT_HASH_TYPE` - (File: `verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates the `hash_type` of each output's **lock script** against the compile-time whitelist `ENABLED_SCRIPT_HASH_TYPE`, but never inspects the output's **type script**. An unprivileged attacker can craft a transaction whose output carries a type script with an unactivated `ScriptHashType` (e.g., `Data3` = `6`), bypass the non-contextual gate, and inject the transaction into the tx-pool. The transaction is only rejected at the later, more expensive script-execution stage, enabling sustained tx-pool pollution at near-zero cost.

## Finding Description

`ENABLED_SCRIPT_HASH_TYPE` is a compile-time `phf_set` containing only the four activated byte values `{0, 1, 2, 4}`: [1](#0-0) 

The `ScriptHashType` enum defines variants up to `Data127` (values `0, 2, 4, 6, 8, …, 254`), all of which parse successfully via `ScriptHashType::try_from()`.

`ScriptHashTypeVerifier::verify()` iterates over `self.transaction.outputs()` and checks only `output.lock().hash_type()`. The `output.type_()` field is never read: [2](#0-1) 

This verifier is the sole non-contextual hash-type gate, invoked inside `NonContextualTransactionVerifier::verify()` at tx-pool admission: [3](#0-2) 

The only enforcement for type scripts with unactivated hash types is the catch-all arm in `select_version()` and `extract_script_and_dep_index()` inside `script/src/types.rs`, which run only during script execution (contextual, block-verification time): [4](#0-3) 

The gap: non-contextual verification (tx-pool admission) does not enforce the whitelist on type scripts; contextual enforcement runs later and only during block verification.

The existing test suite confirms the lock-script path is covered but no analogous test exists for type scripts: [5](#0-4) 

## Impact Explanation

An attacker submits transactions whose outputs carry a type script with `hash_type = 6` (`Data3`, a valid parseable enum variant absent from `ENABLED_SCRIPT_HASH_TYPE`). These transactions pass `NonContextualTransactionVerifier` and enter the tx-pool. Because the transactions are never mined (script execution rejects them), the attacker's UTXOs are never consumed. The attacker can continuously resubmit the same or similar transactions, flooding the tx-pool and propagating invalid transactions via P2P relay to other nodes' pools.

This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** The attacker needs only a small amount of CKB (to reference as inputs) and network access; the tokens are never spent since the transactions are never confirmed.

## Likelihood Explanation

Any unprivileged transaction sender reachable via `send_transaction` RPC or P2P relay can trigger this. No special privileges, keys, or majority hashpower are required. The `ScriptHashType` enum exposes `Data3` through `Data127` as valid parseable values, and the RPC/P2P layer accepts any structurally valid molecule-encoded transaction. The attack is trivially constructable and repeatable at near-zero cost.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the `hash_type` of each output's type script when present:

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

Add a corresponding unit test mirroring `test_not_enabled_hash_type_output_lock` but using a type script with `ScriptHashType::Data3`.

## Proof of Concept

1. Construct a transaction with one output: lock script uses `ScriptHashType::Data` (value `0`, in whitelist), type script uses `ScriptHashType::Data3` (value `6`, not in whitelist).
2. Submit via `send_transaction` RPC.
3. `NonContextualTransactionVerifier` runs `ScriptHashTypeVerifier::verify()`, which checks only the lock script (`Data`, valid) — passes.
4. Transaction is admitted to the tx-pool.
5. Miner's block assembly triggers script execution; `select_version()` hits the catch-all arm and returns `InvalidScriptHashType` — transaction rejected only at this late stage.
6. Attacker's UTXOs remain unspent; repeat indefinitely to sustain tx-pool pollution across the network.

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
