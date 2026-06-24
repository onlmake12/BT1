All cited code references have been verified against the actual repository. Here is the validation result:

---

Audit Report

## Title
`ScriptHashTypeVerifier` Omits Type Script `hash_type` Validation Against `ENABLED_SCRIPT_HASH_TYPE` — (File: `verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates transaction outputs and validates only the **lock** script's `hash_type` against `ENABLED_SCRIPT_HASH_TYPE`. The **type** script's `hash_type` is never read. A transaction whose output carries a structurally valid but consensus-unactivated `hash_type` (e.g., `0x06` = `Data3`) in its type script passes every non-contextual check and enters the contextual-verification pipeline, consuming node resources before being rejected. Because input existence is a contextual check, fabricated inputs suffice to trigger the path at near-zero cost.

## Finding Description

**`ENABLED_SCRIPT_HASH_TYPE`** is `{0, 1, 2, 4}`: [1](#0-0) 

**`ScriptHashTypeVerifier::verify()`** loops over outputs and calls only `output.lock().hash_type()`. There is no branch reading `output.type_()`: [2](#0-1) 

The struct's doc-comment confirms the intentional (but incomplete) scope — *"Check whether output **lock** hash type within enabled range"*: [3](#0-2) 

**Why `check_data()` does not close the gap:** `CellOutputReader::check_data()` does call `self.type_().check_data()`: [4](#0-3) 

But `check_data()` delegates to `ScriptHashType::verify_value(v)`, which accepts any even byte or `1`: [5](#0-4) 

`Data3 = 6` satisfies `6.is_multiple_of(2) == true`. This is a structural/encoding check, not a consensus-activation check, and cannot substitute for the missing `ENABLED_SCRIPT_HASH_TYPE` gate.

**`NonContextualTransactionVerifier::verify()`** runs `ScriptHashTypeVerifier` as its final step and does not check input existence — that is deferred to contextual verification: [6](#0-5) 

**Contextual rejection:** When the transaction reaches contextual verification, `select_version()` hits the catch-all arm for unrecognized hash types and returns an error: [7](#0-6) 

**Note on `CellbaseVerifier`:** The claim that `CellbaseVerifier` has the same gap is correctly retracted. Cellbase outputs with any type script are rejected before the hash-type loop runs: [8](#0-7) 

**Exploit path:**
1. Attacker submits a transaction via `send_transaction` RPC with `output.type_.hash_type = 0x06`.
2. `check_data()` passes: `verify_value(6)` → `true`.
3. `ScriptHashTypeVerifier::verify()` checks only `lock.hash_type` — type script `hash_type` is never tested. Non-contextual check passes.
4. Transaction enters the contextual-verification queue.
5. Input resolution is attempted (DB lookup); with fabricated inputs this fails cheaply. With real UTXOs the attacker controls, resolution succeeds and `select_version()` on the type script hits the catch-all → `Err(InvalidScriptHashType)` → rejected after consuming script-verification resources.
6. Repeated at scale, this constitutes a resource-exhaustion attack against the tx-pool verification pipeline.

## Impact Explanation

**High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

The non-contextual gate is designed to cheaply filter obviously invalid transactions before they enter the expensive contextual pipeline. The missing type-script `hash_type` check creates an asymmetry: an attacker can craft transactions at near-zero cost (no keys, no stake, no mining power) that bypass the cheap gate and consume contextual-verification resources (input resolution, and with real UTXOs, script-group construction and `select_version()` evaluation) before being rejected. Flooding the `send_transaction` RPC with such transactions can exhaust the tx-pool verification pipeline.

## Likelihood Explanation

The entry point is the public `send_transaction` RPC, reachable by any caller. Constructing a transaction with a specific `hash_type` byte requires only standard transaction serialization. No Sybil capability, private keys, or privileged access is needed. The attack is trivially repeatable at scale with distinct fabricated or real inputs.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's `hash_type` when present, mirroring the existing lock-script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock check
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err(TransactionError::InvalidScriptHashType { hash_type: output.lock().hash_type() }.into());
        }

        // ADD: type script check
        if let Some(type_script) = output.type_().to_opt() {
            if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_script.hash_type()) {
                let val: u8 = hash_type.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
                }
            } else {
                return Err(TransactionError::InvalidScriptHashType { hash_type: type_script.hash_type() }.into());
            }
        }
    }
    Ok(())
}
```

Update the `NonContextualTransactionVerifier` doc-comment to reflect that both lock and type script hash types are validated.

## Proof of Concept

```
Transaction {
  inputs:  [ CellInput { previous_output: <any outpoint, existence not checked non-contextually> } ]
  outputs: [
    CellOutput {
      lock: Script { hash_type: 0x01 (Type) },        // passes ScriptHashTypeVerifier
      type: Some(Script { hash_type: 0x06 (Data3) })  // NEVER checked by ScriptHashTypeVerifier
    }
  ]
}
```

Step-by-step:
1. `check_data()` → `verify_value(6)` → `6.is_multiple_of(2)` → `true` ✓
2. `ScriptHashTypeVerifier::verify()` → checks `lock.hash_type() = 1` ∈ `ENABLED_SCRIPT_HASH_TYPE` ✓; `type_.hash_type()` never read ✓
3. `NonContextualTransactionVerifier::verify()` returns `Ok(())`.
4. Transaction enters contextual-verification queue.
5. With fabricated inputs: fails at input resolution (cheap DB lookup). With real UTXOs: `select_version(type_script)` → catch-all arm → `Err(InvalidScriptHashType)`.
6. Repeated at scale with distinct inputs, this floods the verification pipeline with near-zero per-transaction cost to the attacker.

### Citations

**File:** util/constant/src/consensus.rs (L7-11)
```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
```

**File:** verification/src/transaction_verifier.rs (L70-70)
```rust
/// - Check whether output lock hash type within enabled range
```

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

**File:** util/gen-types/src/extension/check_data.rs (L24-27)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
```

**File:** util/gen-types/src/core.rs (L39-41)
```rust
    pub fn verify_value(v: u8) -> bool {
        v.is_multiple_of(2) || v == 1
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

**File:** verification/src/block_verifier.rs (L126-133)
```rust
        // cellbase output type_ must be empty
        if cellbase_transaction
            .outputs()
            .into_iter()
            .any(|output| output.type_().is_some())
        {
            return Err((CellbaseError::InvalidTypeScript).into());
        }
```
