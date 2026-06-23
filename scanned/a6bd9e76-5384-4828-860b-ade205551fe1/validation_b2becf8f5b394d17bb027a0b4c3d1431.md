### Title
`ScriptHashTypeVerifier` Checks Output Lock Script Hash Type But Omits Type Script Hash Type — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier`, the non-contextual verifier that enforces the `ENABLED_SCRIPT_HASH_TYPE` consensus allowlist, iterates over transaction outputs and validates only `output.lock().hash_type()`. It never inspects `output.type_().hash_type()`. An unprivileged transaction submitter can craft an output whose type script carries a structurally valid but consensus-disallowed hash type (e.g., `Data3 = 6`), bypass this early gate entirely, and force the node to proceed to the expensive contextual script-execution phase before the transaction is finally rejected there.

---

### Finding Description

**Root cause — `ScriptHashTypeVerifier::verify` only checks the lock script:**

```rust
// verification/src/transaction_verifier.rs  L796-L814
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) =
            TryInto::<ScriptHashType>::try_into(output.lock().hash_type())   // ← lock only
        {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err((TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            }).into());
        }
    }
    Ok(())
}
```

`output.type_()` is never read here. The companion structural check `check_data()` does cover both scripts:

```rust
// util/gen-types/src/extension/check_data.rs  L24-L28
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()   // structural validity only
    }
}
```

but `check_data()` only verifies that the raw byte is a valid `ScriptHashType` enum discriminant (i.e., even or `1`). `Data3 = 6` satisfies `verify_value(6)` (6 is even), so it passes `check_data()`. The consensus-level allowlist (`ENABLED_SCRIPT_HASH_TYPE = {0,1,2,4}`) is enforced only by `ScriptHashTypeVerifier`, and only for the lock script.

**Allowlist definition:**

```rust
// util/constant/src/consensus.rs  L7-L12
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // Data
    1u8, // Type
    2u8, // Data1
    4u8, // Data2
};
```

`Data3 = 6`, `Data4 = 8`, … are structurally valid but not in this set.

**Error definition confirms the omission is intentional only for lock scripts:**

```rust
// util/types/src/core/error.rs  L223-L228
/// The lock Script hash_type field is invalid.
#[error("InvalidScriptHashType: the lock Script hash_type field is invalid")]
InvalidScriptHashType { hash_type: Byte }
```

The error message itself says "lock Script", confirming the type script is out of scope for this verifier.

**Execution path for a crafted transaction:**

1. Attacker submits (via RPC `send_transaction` or P2P relay) a transaction whose output has a valid lock script hash type and a type script with `hash_type = Data3 (6)`.
2. `NonContextualTransactionVerifier::verify()` calls `ScriptHashTypeVerifier::verify()` → **passes** (lock script is fine).
3. `check_data()` → **passes** (6 is a valid enum value).
4. Transaction enters the tx-pool and proceeds to `ContextualTransactionVerifier`.
5. `TransactionScriptsVerifier` calls `select_version()` on the type script → returns `Err(ScriptError::InvalidVmVersion(3))` or `Err(ScriptError::InvalidScriptHashType(...))`.
6. Transaction is finally rejected — but only after full contextual verification overhead.

---

### Impact Explanation

The non-contextual gate (`ScriptHashTypeVerifier`) is designed to cheaply reject structurally invalid transactions before the expensive script-execution phase. By omitting the type script check, an attacker can force every receiving node to perform contextual verification (cycle accounting, script group construction, VM dispatch) for transactions that should have been dropped at the first gate. This is a resource-exhaustion / DoS vector: a high-rate stream of such transactions wastes CPU and tx-pool processing time on every full node that relays or validates them, with no fee cost to the attacker (the transactions are rejected before fee deduction). No funds can be stolen and no on-chain state is corrupted, but node availability and throughput are degraded.

---

### Likelihood Explanation

The attack requires only the ability to submit transactions via the public RPC (`send_transaction`) or the P2P relay protocol — both are reachable by any unprivileged peer. Constructing a transaction with a `Data3` type script is trivial. No key material, mining power, or privileged access is needed.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify` to also validate the type script hash type for each output, mirroring the existing lock-script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock-script check
        let lock_ht = TryInto::<ScriptHashType>::try_into(output.lock().hash_type())
            .map_err(|_| TransactionError::InvalidScriptHashType { hash_type: output.lock().hash_type() })?;
        let val: u8 = lock_ht.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
        }

        // NEW: type-script check (if present)
        if let Some(type_script) = output.type_().to_opt() {
            let type_ht = TryInto::<ScriptHashType>::try_into(type_script.hash_type())
                .map_err(|_| TransactionError::InvalidScriptHashType { hash_type: type_script.hash_type() })?;
            let val: u8 = type_ht.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        }
    }
    Ok(())
}
```

---

### Proof of Concept

```
1. Build a transaction with one output:
      lock  = Script { hash_type: Data (0), code_hash: <any>, args: <any> }
      type_ = Some(Script { hash_type: 6 (Data3), code_hash: <any>, args: <any> })
      capacity = sufficient

2. Submit via JSON-RPC:
      curl -X POST http://localhost:8114 \
        -H 'Content-Type: application/json' \
        -d '{"id":1,"jsonrpc":"2.0","method":"send_transaction","params":[<tx>,"passthrough"]}'

3. Observe:
   - NonContextualTransactionVerifier passes (ScriptHashTypeVerifier sees only lock hash_type=0 ✓)
   - check_data() passes (hash_type=6 is even ✓)
   - ContextualTransactionVerifier is entered; script execution rejects with InvalidVmVersion/InvalidScriptHashType
   - Node expends contextual-verification resources before rejection

4. Repeat at high rate to exhaust node resources.
```

**Key code references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** util/gen-types/src/extension/check_data.rs (L24-28)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
```

**File:** util/types/src/core/error.rs (L223-228)
```rust
    /// The lock Script hash_type field is invalid.
    #[error("InvalidScriptHashType: the lock Script hash_type field is invalid")]
    InvalidScriptHashType {
        /// The hash type value
        hash_type: Byte,
    },
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
