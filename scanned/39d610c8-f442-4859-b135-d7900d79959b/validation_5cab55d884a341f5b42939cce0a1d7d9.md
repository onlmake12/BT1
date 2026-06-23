### Title
`ScriptHashTypeVerifier` Omits Type Script Hash Type Validation, Allowing Future/Disabled Hash Types to Bypass Non-Contextual Checks — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` in `verification/src/transaction_verifier.rs` validates the `hash_type` field of each output's **lock script** against `ENABLED_SCRIPT_HASH_TYPE`, but performs no equivalent check on the output's **type script**. Any RPC caller can submit a transaction whose output type script carries a future, not-yet-activated `ScriptHashType` (e.g., `Data3` = 6, `Data4` = 8, …). Such a transaction passes every non-contextual gate — including `check_data()` and `ScriptHashTypeVerifier` — and is admitted into the tx-pool verification queue before being rejected during the more expensive contextual (script-execution) phase. This is a direct analog to the reported pattern: a state-transition value is accepted without validating that it falls within the permitted set, causing downstream processing to operate on an invalid value.

---

### Finding Description

**Root cause — missing type-script branch in `ScriptHashTypeVerifier::verify()`**

`ENABLED_SCRIPT_HASH_TYPE` is defined as the four currently activated values:

```
{0 (Data), 1 (Type), 2 (Data1), 4 (Data2)}
``` [1](#0-0) 

`ScriptHashTypeVerifier::verify()` iterates over every output and checks only `output.lock().hash_type()`:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) { ... }
        } else { ... }
    }
    Ok(())
}
``` [2](#0-1) 

`output.type_().hash_type()` is never read. The struct's own doc-comment acknowledges the gap: *"Check whether output **lock** hash type within enabled range."* [3](#0-2) 

**Why the structural `check_data()` guard does not close the gap**

`check_data()` calls `ScriptHashType::verify_value(v)`, which accepts any byte where the low bit is 1 **or** the value is even:

```rust
pub fn verify_value(v: u8) -> bool {
    v.is_multiple_of(2) || v == 1
}
``` [4](#0-3) 

`Data3` = 6 (even), `Data4` = 8 (even), etc. all satisfy `verify_value`, so `check_data()` passes them. `check_data()` is a *structural* (encoding) check, not a *consensus-activation* check. [5](#0-4) 

**Exploit path**

1. Attacker calls `send_transaction` RPC with a transaction whose output has `type_` set to a script with `hash_type = 6` (`Data3`).
2. `check_data()` passes (6 is even → valid encoding).
3. `NonContextualTransactionVerifier::verify()` runs `ScriptHashTypeVerifier::verify()`, which only inspects the lock script — the type script's `hash_type = 6` is never tested against `ENABLED_SCRIPT_HASH_TYPE`. Non-contextual check passes. [6](#0-5) 

4. The transaction enters the tx-pool verification queue.
5. During contextual verification, `select_version()` is called on the type script. `Data3` hits the catch-all arm and returns `Err(ScriptError::InvalidScriptHashType(...))`. [7](#0-6) 

6. The transaction is rejected — but only after consuming contextual-verification resources.

---

### Impact Explanation

- **Incomplete non-contextual gate**: The non-contextual verifier is supposed to be a cheap, complete filter for structurally or consensus-invalid transactions. By omitting the type-script hash-type check, it allows a class of invalid transactions to pass through to the expensive contextual-verification stage.
- **Tx-pool resource exhaustion (DoS)**: An unprivileged RPC caller can flood the node with transactions that carry future hash types in their type scripts. Each transaction passes non-contextual checks, enters the verification queue, and consumes CPU/memory for contextual verification before being rejected. There is no rate-limit specific to this class of invalid transaction at the non-contextual boundary.
- **Asymmetric validation invariant**: Lock scripts and type scripts are both first-class script fields in CKB's cell model. The lock script's hash type is validated against the enabled set; the type script's is not. This asymmetry is a correctness violation that could mask future consensus bugs if the activation logic for new hash types is ever changed.

---

### Likelihood Explanation

The entry point is the public `send_transaction` RPC, reachable by any unprivileged caller with no keys or special permissions. Crafting a transaction with a specific `hash_type` byte in the type script requires only standard transaction construction. The attack requires no Sybil capability, no mining power, and no privileged access.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate `output.type_()` when present, mirroring the existing lock-script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock check ...
        check_hash_type(output.lock().hash_type())?;

        // ADD: type script check
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}
```

The same fix should be applied to the `CellbaseVerifier` in `verification/src/block_verifier.rs`, which similarly checks only `output.lock().hash_type()` for cellbase outputs. [8](#0-7) 

---

### Proof of Concept

```
Transaction {
  outputs: [
    CellOutput {
      lock: Script { hash_type: 0x01 (Type), ... },   // passes ScriptHashTypeVerifier
      type: Some(Script { hash_type: 0x06 (Data3), ... }) // NEVER checked
    }
  ]
}
```

1. `check_data()` → `verify_value(6)` → `6.is_multiple_of(2)` → `true` ✓
2. `ScriptHashTypeVerifier::verify()` → checks `lock.hash_type() = 1` → in `ENABLED_SCRIPT_HASH_TYPE` ✓; `type_.hash_type()` never read ✓
3. Transaction admitted to tx-pool verification queue.
4. `select_version(type_script)` → `ScriptHashType::Data3` → catch-all arm → `Err(InvalidScriptHashType("Data3 not activated"))` → rejected.

The transaction passes the non-contextual gate and consumes contextual-verification resources before rejection. Repeated at scale, this constitutes a targeted resource-exhaustion attack against the tx-pool verification pipeline.

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

**File:** verification/src/transaction_verifier.rs (L70-70)
```rust
/// - Check whether output lock hash type within enabled range
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

**File:** util/gen-types/src/core.rs (L39-41)
```rust
    pub fn verify_value(v: u8) -> bool {
        v.is_multiple_of(2) || v == 1
    }
```

**File:** util/gen-types/src/extension/check_data.rs (L10-13)
```rust
impl<'r> packed::ScriptReader<'r> {
    fn check_data(&self) -> bool {
        core::ScriptHashType::verify_value(self.hash_type().into())
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

**File:** verification/src/block_verifier.rs (L135-144)
```rust
        for output in cellbase_transaction.outputs() {
            if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
                let val: u8 = hash_type.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err((CellbaseError::InvalidOutputLock).into());
                }
            } else {
                return Err((CellbaseError::InvalidOutputLock).into());
            }
        }
```
