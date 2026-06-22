### Title
Incomplete `ScriptHashTypeVerifier` Omits Type Script Hash-Type Validation in Transaction Outputs — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` checks only the **lock script** `hash_type` of each transaction output against `ENABLED_SCRIPT_HASH_TYPE`, completely skipping the **type script** `hash_type`. This is the direct CKB analog of the ChainlinkOracle pattern: a "supported" gate that silently passes inputs it was designed to reject, allowing malformed transactions to propagate through the P2P network and enter the tx-pool before being caught only at the expensive script-execution stage.

---

### Finding Description

`ENABLED_SCRIPT_HASH_TYPE` in `util/constant/src/consensus.rs` permits exactly four values:

```
{0 = Data, 1 = Type, 2 = Data1, 4 = Data2}
``` [1](#0-0) 

The `ScriptHashType` enum is macro-generated for all even values `0, 2, 4, 6, 8, …, 254` plus `1`, giving valid Rust variants `Data3 = 6`, `Data4 = 8`, … `Data127 = 254` that are **not** in `ENABLED_SCRIPT_HASH_TYPE`. [2](#0-1) 

`ScriptHashTypeVerifier::verify()` iterates over outputs and reads only `output.lock().hash_type()`. It never calls `output.type_().to_opt()` and therefore never inspects the type script's `hash_type`:

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
        } else { … }
    }
    Ok(())   // ← type script hash_type never examined
}
``` [3](#0-2) 

This verifier is the sole non-contextual gate for hash-type enforcement and is invoked by `NonContextualTransactionVerifier::verify()`: [4](#0-3) 

The downstream execution path that *would* catch the invalid type script is `select_version()` in `script/src/types.rs`, which returns `InvalidScriptHashType` for any `ScriptHashType` variant not in `{Data, Data1, Data2, Type}`: [5](#0-4) 

But `select_version()` is only reached during contextual script execution — after the transaction has already been admitted to the tx-pool and relayed across the P2P network.

---

### Impact Explanation

An unprivileged transaction sender constructs a transaction whose output carries:
- A valid lock script (`hash_type = Data`, value 0)
- A type script with `hash_type = 6` (`Data3`, not in `ENABLED_SCRIPT_HASH_TYPE`)

**Step 1 — Non-contextual verification passes.** `ScriptHashTypeVerifier` sees only the lock script and returns `Ok(())`. The transaction is accepted into the tx-pool and relayed to peers.

**Step 2 — Contextual verification fails.** `select_version()` returns `InvalidScriptHashType` for value 6, and the transaction is evicted from the tx-pool.

**Consequences:**
1. **Network resource waste:** The malformed transaction is broadcast to all peers before rejection, consuming P2P bandwidth proportional to the number of connected nodes.
2. **Tx-pool CPU waste:** Capacity verification and time-relative checks run before script execution, burning CPU on a transaction that should have been rejected at the non-contextual gate.
3. **Consensus inconsistency risk:** `ContextualTransactionVerifier::verify()` accepts a `skip_script_verify: bool` parameter. Any code path that sets this to `true` (e.g., certain sync or uncle-processing scenarios) would accept the transaction with an invalid type script hash type, while nodes performing full verification would reject it — a potential consensus split vector. [6](#0-5) 

---

### Likelihood Explanation

The entry path requires only submitting a crafted transaction via the standard RPC (`send_transaction`) or P2P relay — no privileged access, no key material, no majority hashpower. The non-contextual check is the first and cheapest gate; bypassing it is trivially reproducible by any tx-pool submitter or RPC caller.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script `hash_type` for each output that carries one:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock script check (unchanged)
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err(TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            }.into());
        }

        // NEW: also check type script hash_type
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
    }
    Ok(())
}
```

---

### Proof of Concept

1. Build a `TransactionView` with one output:
   - lock script: `hash_type = 0` (Data, valid)
   - type script: `hash_type = 6` (Data3, **not** in `ENABLED_SCRIPT_HASH_TYPE`)
2. Call `NonContextualTransactionVerifier::new(&tx, &consensus).verify()`.
3. **Observe:** returns `Ok(())` — the invalid type script hash type is silently ignored.
4. Call `ContextualTransactionVerifier::new(rtx, consensus, dl, env).verify(max_cycles, false)`.
5. **Observe:** returns `Err(InvalidScriptHashType)` — the error surfaces only at script execution.

The gap between steps 3 and 5 is the window in which the malformed transaction is propagated across the network and processed by the tx-pool, confirming the non-contextual gate is incomplete. [7](#0-6) [1](#0-0) [8](#0-7)

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

**File:** util/gen-types/src/core.rs (L9-32)
```rust
seq!(N in 3..=127 {
    /// Specifies how the script `code_hash` is used to match the script code and how to run the code.
    /// The hash type is split into the high 7 bits and the low 1 bit,
    /// when the low 1 bit is 1, it indicates the type,
    /// when the low 1 bit is 0, it indicates the data,
    /// and then it relies on the high 7 bits to indicate
    /// that the data actually corresponds to the version.
     #[derive(Default, Clone, Copy, PartialEq, Eq, Debug, Hash, FromRepr)]
     #[repr(u8)]
    pub enum ScriptHashType {
        /// Type "type" matches script code via cell type script hash.
        Type = 1,
        /// Type "data" matches script code via cell data hash, and run the script code in v0 CKB VM.
        #[default]
        Data = 0,
        /// Type "data1" matches script code via cell data hash, and run the script code in v1 CKB VM.
        Data1 = 2,
        /// Type "data2" matches script code via cell data hash, and run the script code in v2 CKB VM.
        Data2 = 4,
        #(
            #[doc = concat!("Type \"data", stringify!(N), "\" matches script code via cell data hash, and runs the script code in v", stringify!(N), " CKB VM.")]
            Data~N = N << 1,
        )*
    }
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
