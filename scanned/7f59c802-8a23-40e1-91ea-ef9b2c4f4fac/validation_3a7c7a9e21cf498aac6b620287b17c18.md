Audit Report

## Title
`ScriptHashTypeVerifier` Omits Type Script `hash_type` Validation in Transaction Outputs — (File: `verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates only the **lock script** `hash_type` against `ENABLED_SCRIPT_HASH_TYPE`, never inspecting the **type script** `hash_type`. A transaction output carrying a valid lock script `hash_type` (e.g., `0`) and an invalid type script `hash_type` (e.g., `6`, `Data3`) passes `NonContextualTransactionVerifier` silently. The error surfaces only at script execution inside `ContextualTransactionVerifier`, after the transaction has already been admitted to the tx-pool processing pipeline and potentially relayed to peers.

## Finding Description

`ENABLED_SCRIPT_HASH_TYPE` in `util/constant/src/consensus.rs` (lines 7–11) permits exactly `{0, 1, 2, 4}`. The `ScriptHashType` enum includes additional even variants (`6 = Data3`, `8 = Data4`, … `254 = Data127`) that are valid Rust enum values but are not in `ENABLED_SCRIPT_HASH_TYPE`.

`ScriptHashTypeVerifier::verify()` at `verification/src/transaction_verifier.rs` lines 796–814 reads only `output.lock().hash_type()`:

```rust
for output in self.transaction.outputs() {
    if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
        let val: u8 = hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) { … }
    } else { … }
}
Ok(())  // type script hash_type never examined
``` [1](#0-0) 

It never calls `output.type_().to_opt()`, so a type script with `hash_type = 6` passes without error.

This verifier is the sole non-contextual gate for hash-type enforcement, called by `NonContextualTransactionVerifier::verify()` at line 100: [2](#0-1) 

The downstream catch is `select_version()` in `script/src/types.rs` lines 930–935, which returns `InvalidScriptHashType` for any variant outside `{Data, Data1, Data2, Type}`: [3](#0-2) 

However, `select_version()` is only reached during contextual script execution. `ContextualTransactionVerifier::verify()` accepts a `skip_script_verify: bool` parameter (line 162); any code path setting this to `true` — confirmed present in `verification/contextual/src/contextual_block_verifier.rs` — would accept the transaction with an invalid type script `hash_type` entirely, while full-verifying nodes reject it. [4](#0-3) 

## Impact Explanation

**High — bad design which could cause CKB network congestion with few costs.**

An unprivileged attacker can cheaply craft transactions with a valid lock `hash_type` and an invalid type `hash_type` (e.g., `6`). These pass the non-contextual gate and enter the tx-pool processing pipeline, consuming capacity verification and time-relative check CPU before being evicted at script execution. Because the cost to produce such transactions is negligible (no key material, no hashpower, standard RPC access), an attacker can sustain a stream of them to waste tx-pool CPU and, depending on relay ordering relative to contextual verification, P2P bandwidth across all connected peers. The secondary `skip_script_verify` path introduces a potential consensus split vector between nodes that skip and nodes that perform full script verification.

## Likelihood Explanation

Triggerable by any unprivileged user via `send_transaction` RPC or P2P relay. No special privileges, key material, or majority hashpower required. The crafted transaction requires only setting `hash_type = 6` in the type script field of any output. Fully reproducible and repeatable at negligible cost.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script `hash_type` for each output that carries one, mirroring the existing lock script check:

```rust
if let Some(type_script) = output.type_().to_opt() {
    if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_script.hash_type()) {
        let val: u8 = hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
        }
    } else {
        return Err(TransactionError::InvalidScriptHashType {
            hash_type: type_script.hash_type(),
        }.into());
    }
}
```

This ensures the non-contextual gate is complete and consistent with the intent of `ENABLED_SCRIPT_HASH_TYPE`. [5](#0-4) 

## Proof of Concept

1. Build a `TransactionView` with one output: lock script `hash_type = 0` (Data, valid), type script `hash_type = 6` (Data3, not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Call `NonContextualTransactionVerifier::new(&tx, &consensus).verify()`.
3. **Observe:** returns `Ok(())` — the invalid type script `hash_type` is silently ignored.
4. Call `ContextualTransactionVerifier::new(rtx, consensus, dl, env).verify(max_cycles, false)`.
5. **Observe:** returns `Err(InvalidScriptHashType)` at script execution.

The gap between steps 3 and 5 is the window in which the malformed transaction is processed by the tx-pool pipeline and potentially relayed, confirming the non-contextual gate is incomplete. [6](#0-5)

### Citations

**File:** verification/src/transaction_verifier.rs (L94-102)
```rust
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

**File:** verification/src/transaction_verifier.rs (L162-172)
```rust
    pub fn verify(&self, max_cycles: Cycle, skip_script_verify: bool) -> Result<Completed, Error> {
        self.time_relative.verify()?;
        self.capacity.verify()?;
        let cycles = if skip_script_verify {
            0
        } else {
            self.script.verify(max_cycles)?
        };
        let fee = self.fee_calculator.transaction_fee()?;
        Ok(Completed { cycles, fee })
    }
```

**File:** verification/src/transaction_verifier.rs (L787-815)
```rust
pub struct ScriptHashTypeVerifier<'a> {
    transaction: &'a TransactionView,
}

impl<'a> ScriptHashTypeVerifier<'a> {
    pub fn new(transaction: &'a TransactionView) -> Self {
        Self { transaction }
    }

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

**File:** util/constant/src/consensus.rs (L7-11)
```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
```
