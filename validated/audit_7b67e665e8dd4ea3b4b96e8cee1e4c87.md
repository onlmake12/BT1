### Title
`ScriptHashTypeVerifier` Skips Hash-Type Enforcement for Output Type Scripts, Allowing Unenabled Hash Types to Bypass Early Rejection — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` enforces the `ENABLED_SCRIPT_HASH_TYPE` allowlist only against each output's **lock script**, silently skipping the same check for each output's **type script**. An unprivileged tx-pool submitter can craft a transaction whose output carries a type script with an unenabled hash type (e.g., `Data3` = 6, `Data4` = 8, …, `Data127` = 254). Such a transaction passes every fast non-contextual check and enters the tx pool, where it is only rejected later during the expensive full script-execution stage — wasting node CPU and memory.

---

### Finding Description

`ENABLED_SCRIPT_HASH_TYPE` is the consensus-level allowlist of currently active hash types:

```
{0 = Data, 1 = Type, 2 = Data1, 4 = Data2}
``` [1](#0-0) 

The structural `check_data` gate (used during P2P message parsing) accepts any hash-type byte whose low bit is 0 or whose value is 1 — i.e., every even number and 1. This means `Data3` (6), `Data4` (8), … `Data127` (254) all pass structural validation: [2](#0-1) 

`ScriptHashTypeVerifier` is the dedicated verifier whose stated purpose is *"Verify that the ScriptHashType of transaction outputs is within the range permitted by the current consensus rules."* Its `verify()` loop iterates over outputs and checks `output.lock().hash_type()` against `ENABLED_SCRIPT_HASH_TYPE`, but **never reads `output.type_()`**: [3](#0-2) 

The type script's hash type is therefore never validated by this verifier. The only place an unenabled type-script hash type is eventually caught is deep inside `TxInfo::select_version`, which is invoked during full script execution: [4](#0-3) 

This is the exact structural analog to the external report: one category (lock scripts) receives the type-check; the parallel category (type scripts) does not, in the same verifier.

---

### Impact Explanation

A transaction with an output type script whose `hash_type` byte is 6 (`Data3`) passes:
1. P2P / RPC structural parsing (`check_data` — even number accepted)
2. `ScriptHashTypeVerifier` (only checks lock scripts)
3. All other non-contextual verifiers

It is only rejected when the node attempts full script execution (`select_version` → `InvalidScriptHashType`). This forces the node to perform expensive work — cell resolution, dep loading, VM setup — before the transaction is discarded. An attacker who floods the tx-pool submission endpoint with such transactions can exhaust CPU and memory on the victim node without paying any fee, because the transactions never commit.

---

### Likelihood Explanation

The attack requires no special privilege: any peer or RPC caller can submit transactions via `send_transaction`. Constructing a transaction with an output type script whose `hash_type` = 6 is trivial. The `ScriptHashType` enum already defines `Data3 = 6` as a valid Rust variant, and the RPC JSON type accepts it. The gap between `check_data` (accepts even bytes) and `ENABLED_SCRIPT_HASH_TYPE` (only {0,1,2,4}) is wide enough to accommodate many such values.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type for each output, mirroring the existing lock-script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock check
        let lock_ht = TryInto::<ScriptHashType>::try_into(output.lock().hash_type())
            .map_err(|_| TransactionError::InvalidScriptHashType { hash_type: output.lock().hash_type() })?;
        let val: u8 = lock_ht.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
        }

        // NEW: same check for type script
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

1. Craft a transaction with one output whose lock script uses `hash_type = 1` (Type, valid) and whose type script uses `hash_type = 6` (Data3, unenabled).
2. Submit via `send_transaction` RPC.
3. Observe: `check_data` passes (6 is even); `ScriptHashTypeVerifier` passes (only checks lock); the transaction enters the tx pool.
4. Observe: the node begins script execution, reaches `select_version` with `Data3`, and only then returns `InvalidScriptHashType`.
5. Repeat at high rate to exhaust node resources.

The root cause is at: [5](#0-4) 

where `output.type_()` is never consulted, while the analogous lock-script path at line 798 is checked. The `ENABLED_SCRIPT_HASH_TYPE` set that should gate both paths is defined at: [1](#0-0)

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

**File:** util/gen-types/src/extension/check_data.rs (L10-27)
```rust
impl<'r> packed::ScriptReader<'r> {
    fn check_data(&self) -> bool {
        core::ScriptHashType::verify_value(self.hash_type().into())
    }
}

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
