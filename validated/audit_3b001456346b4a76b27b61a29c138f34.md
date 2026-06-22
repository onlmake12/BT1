### Title
`ScriptHashTypeVerifier` Enforces `ENABLED_SCRIPT_HASH_TYPE` on Output Lock Scripts but Not Output Type Scripts - (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates that each output's **lock script** hash type is within the `ENABLED_SCRIPT_HASH_TYPE` set, but it never checks the **type script** hash type of the same outputs. This is a direct analog to the ERC20 `approve()` missing `transferSanity()` pattern: a check is consistently applied to one field (lock) but silently omitted for a parallel field (type), breaking the stated invariant that all script hash types in outputs must be within the permitted range.

---

### Finding Description

`ScriptHashTypeVerifier` is documented as: *"Verify that the ScriptHashType of transaction outputs is within the range permitted by the current consensus rules."*

Its implementation in `verification/src/transaction_verifier.rs` (lines 796–814):

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

Only `output.lock().hash_type()` is checked. `output.type_()` — the optional type script — is never inspected against `ENABLED_SCRIPT_HASH_TYPE`.

A `CellOutput` has both a mandatory lock script and an optional type script:

```
table CellOutput {
    capacity:  Uint64,
    lock:      Script,      // ← checked
    type_:     ScriptOpt,   // ← NOT checked
}
```

The `check_data()` function in `util/gen-types/src/extension/check_data.rs` (lines 24–28) does validate both fields, but only for structural validity (is the byte a known enum variant?), not for whether the hash type is within the currently-enabled consensus range. The `ENABLED_SCRIPT_HASH_TYPE` enforcement — the stricter, consensus-level gate — is missing for type scripts.

This verifier is called from `NonContextualTransactionVerifier::verify()` (line 100) and from `NonContextualBlockTxsVerifier::verify()` (line 284), which is invoked for every block received from peers in `ChainService::non_contextual_verify()` (line 79). It is also called in the tx-pool's `non_contextual_verify()` (line 60 of `tx-pool/src/util.rs`).

---

### Impact Explanation

An attacker or user can craft a transaction whose output carries a type script with a future/disallowed hash type (e.g., `ScriptHashType::Data3`, `Data4`, …). This transaction:

1. **Passes `NonContextualTransactionVerifier`** — `ScriptHashTypeVerifier` only checks the lock script.
2. **Passes `ContextualTransactionVerifier`** — type scripts are only *executed* when the cell is consumed as an input, not when it is created as an output. No script execution occurs at creation time.
3. **Is included in a block** — the cell is committed to the chain.
4. **Creates a permanently unspendable cell** — any subsequent transaction attempting to consume this cell will invoke `select_version()` on the type script, which returns `Err(ScriptError::InvalidVmVersion(N))` for any not-yet-activated `DataN` hash type, causing the spending transaction to fail script verification unconditionally.

The result is a permanent, irrecoverable loss of the CKB capacity locked in that cell. The protocol invariant — that all script hash types in committed outputs are within the enabled range — is broken for type scripts.

---

### Likelihood Explanation

Any unprivileged transaction sender can trigger this. No special access, key material, or majority hashpower is required. The attacker simply constructs a transaction output with a type script whose `hash_type` byte is set to a valid-but-not-enabled value (e.g., `0x06` for `Data3`). The transaction is submitted via the standard `send_transaction` RPC or relayed over P2P. The non-contextual check passes, the tx enters the pool, and a miner includes it in a block.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type for each output, mirroring the existing lock script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // Check lock script hash type (existing)
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

        // Check type script hash type (missing — add this)
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

**Root cause — missing type script check:** [1](#0-0) 

The verifier only calls `output.lock().hash_type()` and never touches `output.type_()`.

**The invariant is stated in the doc comment but not enforced for type scripts:** [2](#0-1) 

**`CellOutput` has both lock and type fields; only lock is checked:** [3](#0-2) 

**`check_data()` validates both fields for structural validity but not for `ENABLED_SCRIPT_HASH_TYPE`:** [4](#0-3) 

**`ScriptHashTypeVerifier` is called in the non-contextual block tx verifier, which runs for every peer-relayed block:** [5](#0-4) 

**And in the tx-pool non-contextual path, reachable by any RPC caller:** [6](#0-5) 

**`select_version()` confirms that a `Data3` type script would fail at execution time with `InvalidVmVersion`, making the cell permanently unspendable:** [7](#0-6)

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

**File:** util/gen-types/schemas/blockchain.mol (L46-50)
```text
table CellOutput {
    capacity:       Uint64,
    lock:           Script,
    type_:          ScriptOpt,
}
```

**File:** util/gen-types/src/extension/check_data.rs (L24-28)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
```

**File:** verification/src/block_verifier.rs (L280-286)
```rust
    pub fn verify(&self, block: &BlockView) -> Result<Vec<()>, Error> {
        block
            .transactions()
            .iter()
            .map(|tx| NonContextualTransactionVerifier::new(tx, self.consensus).verify())
            .collect()
    }
```

**File:** tx-pool/src/util.rs (L56-63)
```rust
pub(crate) fn non_contextual_verify(
    consensus: &Consensus,
    tx: &TransactionView,
) -> Result<(), Reject> {
    NonContextualTransactionVerifier::new(tx, consensus)
        .verify()
        .map_err(Reject::Verification)?;

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
