Audit Report

## Title
`ScriptHashTypeVerifier::verify()` Never Inspects Output Type Script Hash Types, Allowing Consensus-Disabled Hash Types to Bypass Activation Gate — (File: `verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates only `output.lock().hash_type()` against `ENABLED_SCRIPT_HASH_TYPE`. The `output.type_()` field is never read. An attacker can embed a consensus-disabled hash type (e.g., `Data3 = 6`) in an output's type script, pass this verifier without error, and — during CKB's staged hardfork window when the VM binary already supports the target version but consensus has not yet activated it — cause nodes running the new binary to accept the transaction while nodes running the old binary reject it, producing a consensus split.

## Finding Description

**Root cause — `ScriptHashTypeVerifier::verify()` lines 796–814:**

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) =
            TryInto::<ScriptHashType>::try_into(output.lock().hash_type())
        {
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
``` [1](#0-0) 

Every iteration calls `output.lock().hash_type()` exclusively. There is no call to `output.type_().to_opt()` anywhere in this function. The comment on the struct itself states the intent is to verify "the ScriptHashType of transaction outputs is within the range permitted by the current consensus rules" — but only half of each output is checked. [2](#0-1) 

`ENABLED_SCRIPT_HASH_TYPE` is `{0, 1, 2, 4}` — Data, Type, Data1, Data2 only: [3](#0-2) 

A lock script with `hash_type = 6` is caught and rejected at line 800–803. A type script with `hash_type = 6` is invisible to this verifier entirely.

**Exploit flow:**

1. Attacker constructs a transaction whose output has a lock script with `hash_type = 0` (Data, allowed) and a type script with `hash_type = 6` (Data3, not in `ENABLED_SCRIPT_HASH_TYPE`).
2. `ScriptHashTypeVerifier::verify()` reads only the lock script hash type, finds `0 ∈ ENABLED_SCRIPT_HASH_TYPE`, and returns `Ok(())`.
3. `TransactionScriptsVerifier` runs the type script group. On a node whose CKB-VM binary already supports VM version 3 (the normal state during hardfork preparation), `select_version` succeeds and the script executes — transaction accepted.
4. On a node whose binary does not yet support VM version 3, `select_version` returns `InvalidVmVersion(3)` — transaction rejected.
5. The two nodes disagree on chain state.

## Impact Explanation

**Critical — consensus deviation (15001–25000 points).** CKB's staged hardfork model deliberately ships VM support in the binary before the consensus switch activates it; `ScriptHashTypeVerifier` is the enforcement point that keeps the two in sync. Because it ignores type scripts, any transaction sender can unilaterally activate a future VM version for type scripts during the preparation window. Nodes running the new binary accept the transaction; nodes running the old binary reject it. This is a chain-splitting condition reachable by any unprivileged user with no special access.

## Likelihood Explanation

The attacker is an ordinary transaction sender — no privileged access, no keys, no majority hashpower required. The only precondition is that the CKB-VM binary already contains support for the target VM version, which is the normal and expected state during any hardfork preparation window. The crafted transaction is submitted via the standard `send_transaction` RPC. The gap is reachable on every transaction that includes an output with a type script.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to validate the hash type of each output's type script in addition to its lock script:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        Self::check_hash_type(output.lock().hash_type())?;
        if let Some(type_script) = output.type_().to_opt() {
            Self::check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}

fn check_hash_type(raw: packed::Byte) -> Result<(), Error> {
    match TryInto::<ScriptHashType>::try_into(raw) {
        Ok(hash_type) => {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
            Ok(())
        }
        Err(_) => Err((TransactionError::InvalidScriptHashType { hash_type: raw }).into()),
    }
}
```

## Proof of Concept

1. Build a `TransactionView` with one output: lock script `hash_type = 0` (Data), type script `hash_type = 6` (Data3).
2. Instantiate `ScriptHashTypeVerifier::new(&tx)` and call `.verify()`.
3. Observe `Ok(())` is returned — the type script hash type is never inspected.
4. Submit the transaction via `send_transaction` RPC to a node whose CKB-VM binary supports VM version 3.
5. Observe the transaction is accepted on that node.
6. Submit the same transaction to a node whose binary does not support VM version 3.
7. Observe `InvalidVmVersion(3)` — transaction rejected.
8. The two nodes now hold divergent chain state — consensus split confirmed.

A unit test can be added directly alongside the existing `ScriptHashTypeVerifier` tests in `verification/src/tests/transaction_verifier.rs` to assert that a transaction with a type script carrying `hash_type = 6` returns `Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: 6 })`. [4](#0-3)

### Citations

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

**File:** util/constant/src/consensus.rs (L7-12)
```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
};
```

**File:** verification/src/tests/transaction_verifier.rs (L1-7)
```rust
use super::super::transaction_verifier::{
    CapacityVerifier, DaoScriptSizeVerifier, DuplicateDepsVerifier, EmptyVerifier,
    MaturityVerifier, OutputsDataVerifier, Since, SinceVerifier, SizeVerifier, VersionVerifier,
};
use crate::error::TransactionErrorSource;
use crate::transaction_verifier::ScriptHashTypeVerifier;
use crate::{TransactionError, TxVerifyEnv};
```
