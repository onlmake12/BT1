### Title
`ScriptHashTypeVerifier` Only Validates Lock Script Hash Type, Silently Skipping Type Script — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` is intended to enforce that all output scripts in a transaction use only currently-enabled `ScriptHashType` values (the set `{Data=0, Type=1, Data1=2, Data2=4}`). However, the verifier only inspects `output.lock().hash_type()` and never checks `output.type_()`. An unprivileged transaction sender can craft an output whose **type script** carries a future/reserved hash type (e.g., `Data3 = 6`) and the transaction will pass all non-contextual checks, enter the tx-pool, and be relayed across the network before failing at the expensive script-execution stage.

---

### Finding Description

`ScriptHashTypeVerifier::verify()` iterates over every output and validates only the lock script:

```rust
// verification/src/transaction_verifier.rs  lines 796-814
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

There is no corresponding check for `output.type_()`. The enabled set is:

```rust
// util/constant/src/consensus.rs  lines 7-12
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
};
```

The lower-level `check_data()` function does visit both lock and type scripts, but it only calls `ScriptHashType::verify_value()`, which accepts any even byte or the value `1` — it does **not** enforce membership in `ENABLED_SCRIPT_HASH_TYPE`:

```rust
// util/gen-types/src/extension/check_data.rs  lines 24-27
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
```

```rust
// util/gen-types/src/core.rs  lines 39-41
pub fn verify_value(v: u8) -> bool {
    v.is_multiple_of(2) || v == 1
}
```

`Data3 = 6` (and `Data4 = 8`, etc.) are even numbers, so they pass `check_data()` and also pass `ScriptHashTypeVerifier` (which never reads the type script). The transaction therefore clears the entire `NonContextualTransactionVerifier` pipeline:

```rust
// verification/src/transaction_verifier.rs  lines 94-101
pub fn verify(&self) -> Result<(), Error> {
    self.version.verify()?;
    self.size.verify()?;
    self.empty.verify()?;
    self.duplicate_deps.verify()?;
    self.outputs_data_verifier.verify()?;
    self.script_hash_type.verify()?;   // ← passes; type script never checked
    Ok(())
}
```

The error is only surfaced later inside `TxInfo::select_version()` during script execution:

```rust
// script/src/types.rs  lines 930-935
hash_type => {
    return Err(ScriptError::InvalidScriptHashType(format!(
        "The ScriptHashType/{:?} has not been activated, and is not permitted for use.",
        hash_type
    )));
}
```

---

### Impact Explanation

A transaction with a type script carrying `Data3` (byte value `6`) passes every cheap, early gate (`check_data`, `NonContextualTransactionVerifier`) and is admitted to the tx-pool and relayed to peers. The rejection only occurs at the script-execution stage, which is the most resource-intensive part of transaction validation. An attacker can flood the network with such transactions, forcing every receiving node to perform expensive contextual verification and script-group setup before finally discarding the transaction. This constitutes a **resource-exhaustion / tx-pool DoS** reachable by any unprivileged RPC caller or P2P peer.

---

### Likelihood Explanation

The attack requires only the ability to submit a transaction via the public `send_transaction` RPC or via P2P relay — no keys, no stake, no privileged access. The crafted transaction is small and cheap to produce. The gap between `check_data` (accepts any even byte) and `ENABLED_SCRIPT_HASH_TYPE` (only four values) is wide enough that future hash types (`Data3`, `Data4`, …, `Data127`) all qualify as bypass values. Likelihood is **high**.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script of each output, mirroring the lock-script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock-script check
        let lock_hash_type: ScriptHashType = output.lock().hash_type().try_into()?;
        let val: u8 = lock_hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) { ... }

        // NEW: type-script check
        if let Some(type_script) = output.type_().to_opt() {
            let type_hash_type: ScriptHashType = type_script.hash_type().try_into()
                .map_err(|_| TransactionError::InvalidScriptHashType { ... })?;
            let val: u8 = type_hash_type.into();
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

1. Construct a transaction output with:
   - `lock`: any valid script with `hash_type = Data` (value `0`) — passes `ScriptHashTypeVerifier`.
   - `type_`: a script with `hash_type = 6` (`Data3`) — never checked by `ScriptHashTypeVerifier`.
2. Submit via `send_transaction` RPC.
3. Observe the transaction passes `NonContextualTransactionVerifier` (including `ScriptHashTypeVerifier`) and enters the tx-pool.
4. Observe the transaction is only rejected later, inside `TxInfo::select_version()` / `extract_script_and_dep_index()`, after expensive contextual verification has already been performed.

**Root cause files:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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
