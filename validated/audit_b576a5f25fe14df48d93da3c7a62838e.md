Audit Report

## Title
Incomplete Script Hash Type Validation in `ScriptHashTypeVerifier` Omits Type Script Check — (File: `verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` validates the `hash_type` field of each output's **lock** script but never inspects the optional **type** script's `hash_type`. An attacker can craft transactions whose type script carries an invalid or consensus-unsupported `hash_type` byte (e.g., `0xFF`), bypass the non-contextual gate entirely, and have those transactions admitted to the tx-pool and relayed across the P2P network — consuming pool memory and relay bandwidth on every peer — at zero on-chain cost.

## Finding Description
`NonContextualTransactionVerifier::verify()` runs six sub-verifiers in sequence, with `ScriptHashTypeVerifier` as the final gate: [1](#0-0) 

`ScriptHashTypeVerifier::verify()` iterates over every output and checks **only** `output.lock().hash_type()`: [2](#0-1) 

`ENABLED_SCRIPT_HASH_TYPE` is defined as `{0, 1, 2, 4}`: [3](#0-2) 

The verifier never calls `output.type_().to_opt()`, so any output whose type script carries `hash_type = 0xFF` (or any byte outside `{0,1,2,4}`) passes this check unconditionally. Grep confirms there is no call to `type_()` anywhere in the non-contextual verification path within `transaction_verifier.rs`. The `NonContextualTransactionVerifier` is invoked at tx-pool admission time: [4](#0-3) 

## Impact Explanation
Transactions with invalid type script `hash_type` values pass non-contextual verification, enter the tx-pool, and are relayed to all connected peers. Each peer repeats the same admission and relay cycle. The transactions can never be mined into a valid block, but they occupy tx-pool memory and consume relay bandwidth on every node until contextual verification eventually discards them. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"** (10001–15000 points). An attacker can sustain the attack indefinitely at zero fee cost since the transactions are never confirmed.

## Likelihood Explanation
The entry path is fully unprivileged — any caller of the `send_raw_transaction` RPC or any P2P transaction relay participant can trigger this. Crafting the malicious transaction requires only setting a single byte (`hash_type`) in the type script to `0xFF`. The CKB serialization format is public and well-documented. No special privileges, leaked keys, or victim mistakes are required. The attack is trivially repeatable and scriptable.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to also validate the `hash_type` of each output's optional type script, mirroring the existing lock script check:

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

where `check_hash_type` encapsulates the existing `TryInto<ScriptHashType>` conversion and `ENABLED_SCRIPT_HASH_TYPE` membership test from lines 798–810. [2](#0-1) 

## Proof of Concept
1. Build a `TransactionView` with one output whose `lock` script has `hash_type = 0x01` (valid `Type`) and whose `type_` script has `hash_type = 0xFF`.
2. Call `NonContextualTransactionVerifier::new(&tx, &consensus).verify()`.
3. Observe `Ok(())` — all six non-contextual checks pass.
4. Submit via `send_raw_transaction` RPC; the transaction is accepted into the tx-pool.
5. Observe the transaction being relayed to peers and consuming pool memory until contextual verification rejects it.

The root cause is confirmed at `verification/src/transaction_verifier.rs` lines 797–811, where `output.type_()` is never accessed.

### Citations

**File:** verification/src/transaction_verifier.rs (L93-102)
```rust
    /// Perform context-independent verification
    pub fn verify(&self) -> Result<(), Error> {
        self.version.verify()?;
        self.size.verify()?;
        self.empty.verify()?;
        self.duplicate_deps.verify()?;
        self.outputs_data_verifier.verify()?;
        self.script_hash_type.verify()?;
        Ok(())
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

**File:** tx-pool/src/util.rs (L1-5)
```rust
use crate::error::Reject;
use crate::pool::TxPool;
use ckb_chain_spec::consensus::Consensus;
use ckb_dao::DaoCalculator;
use ckb_script::ChunkCommand;
```
