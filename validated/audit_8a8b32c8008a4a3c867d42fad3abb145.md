### Title
`ScriptHashTypeVerifier` Omits Type Script Hash-Type Check, Allowing Disallowed Hash Types to Bypass Early Rejection Gate — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` is the non-contextual gate that rejects transactions whose output scripts use a `ScriptHashType` not yet permitted by consensus. It iterates every output and validates `output.lock().hash_type()`, but never inspects `output.type_().hash_type()`. An unprivileged transaction sender can therefore craft a transaction whose type script carries a disallowed hash type (e.g., `Data3`), bypass the cheap early-rejection check, and force the node to perform expensive script-execution verification before the transaction is ultimately rejected.

---

### Finding Description

`ScriptHashTypeVerifier::verify()` in `verification/src/transaction_verifier.rs` reads:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(...)
            }
        } else {
            return Err(...)
        }
    }
    Ok(())
}
``` [1](#0-0) 

The loop body calls `output.lock().hash_type()` exclusively. `output.type_()` is never read. The struct comment at line 785 states the intent is to verify *"the ScriptHashType of transaction outputs"* — both lock and type scripts — against `ENABLED_SCRIPT_HASH_TYPE`. [2](#0-1) 

`ScriptHashTypeVerifier` is composed into `NonContextualTransactionVerifier`, which is the first, cheapest verification stage applied to every submitted transaction: [3](#0-2) 

The downstream script-execution path (`TransactionScriptsVerifier::select_version`) does enforce hash-type gating per script, so a transaction with a disallowed type-script hash type is ultimately rejected — but only after expensive VM setup and cycle accounting: [4](#0-3) 

The molecule-level `check_data` helper correctly validates both lock and type scripts for syntactic validity, but that is a separate, lower-level check and does not enforce the consensus-permitted set: [5](#0-4) 

The analog to the reported Solidity bug is exact: `onlyMinter` compared `formImplementationId` instead of the correct `stateRegistryId`; here `ScriptHashTypeVerifier` reads `lock().hash_type()` instead of also reading `type_().hash_type()` — the wrong field is checked, leaving the intended invariant unenforced for type scripts.

---

### Impact Explanation

An attacker submits a stream of transactions whose outputs carry a type script with a not-yet-activated `ScriptHashType` (e.g., `ScriptHashType::Data3 = 6`). Each transaction:

1. Passes `NonContextualTransactionVerifier` (the cheap gate) because `ScriptHashTypeVerifier` never inspects the type script.
2. Proceeds into contextual verification and full script execution inside the tx-pool admission path.
3. Is rejected only after the node has paid the cost of VM initialization and `select_version` evaluation.

This creates a **resource-exhaustion / DoS** vector: the attacker can force the node to perform O(script-execution-setup) work per transaction instead of O(byte-comparison) work. At scale, this degrades tx-pool throughput and can starve legitimate transaction processing. The gap also means the non-contextual check gives a false guarantee of completeness to any downstream component that relies on it.

---

### Likelihood Explanation

The attack requires no privileged access, no keys, and no special network position. Any RPC caller or P2P peer can submit raw transactions. Crafting a transaction with an invalid type-script hash type is trivial — it is a single-byte field in the serialized script. The attacker needs only to know which `ScriptHashType` values are outside `ENABLED_SCRIPT_HASH_TYPE`, which is public consensus configuration. Likelihood is **high**.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script of each output, mirroring the existing lock-script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // Check lock script hash type
        let lock_hash_type = TryInto::<ScriptHashType>::try_into(output.lock().hash_type())
            .map_err(|_| TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            })?;
        let val: u8 = lock_hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
        }

        // Check type script hash type (previously missing)
        if let Some(type_script) = output.type_().to_opt() {
            let type_hash_type = TryInto::<ScriptHashType>::try_into(type_script.hash_type())
                .map_err(|_| TransactionError::InvalidScriptHashType {
                    hash_type: type_script.hash_type(),
                })?;
            let val: u8 = type_hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(
                    TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into(),
                );
            }
        }
    }
    Ok(())
}
```

This aligns with the molecule-level `CellOutputReader::check_data()` which already validates both `lock()` and `type_()` for structural correctness.

---

### Proof of Concept

1. Construct a transaction output with a lock script using `ScriptHashType::Data` (valid) and a type script using `ScriptHashType::Data3` (value `6`, not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Submit the transaction via RPC (`send_raw_transaction`) or P2P relay.
3. Observe that `NonContextualTransactionVerifier::verify()` returns `Ok(())` — the transaction passes the cheap gate.
4. The transaction proceeds to contextual verification; `TransactionScriptsVerifier::select_version()` returns `Err(ScriptError::InvalidVmVersion(3))` and the transaction is rejected only at that expensive stage.
5. Repeat at high frequency to exhaust tx-pool admission CPU budget.

Expected result with fix: step 3 returns `Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: 6 })` immediately, before any script-execution work is performed.

### Citations

**File:** verification/src/transaction_verifier.rs (L61-102)
```rust
/// Context-independent verification checks for transaction
///
/// Basic checks that don't depend on any context
/// Contains:
/// - Check for version
/// - Check for size
/// - Check inputs and output empty
/// - Check for duplicate deps
/// - Check for whether outputs match data
/// - Check whether output lock hash type within enabled range
pub struct NonContextualTransactionVerifier<'a> {
    pub(crate) version: VersionVerifier<'a>,
    pub(crate) size: SizeVerifier<'a>,
    pub(crate) empty: EmptyVerifier<'a>,
    pub(crate) duplicate_deps: DuplicateDepsVerifier<'a>,
    pub(crate) outputs_data_verifier: OutputsDataVerifier<'a>,
    pub(crate) script_hash_type: ScriptHashTypeVerifier<'a>,
}

impl<'a> NonContextualTransactionVerifier<'a> {
    /// Creates a new NonContextualTransactionVerifier
    pub fn new(tx: &'a TransactionView, consensus: &'a Consensus) -> Self {
        NonContextualTransactionVerifier {
            version: VersionVerifier::new(tx, consensus.tx_version()),
            size: SizeVerifier::new(tx, consensus.max_block_bytes()),
            empty: EmptyVerifier::new(tx),
            duplicate_deps: DuplicateDepsVerifier::new(tx),
            outputs_data_verifier: OutputsDataVerifier::new(tx),
            script_hash_type: ScriptHashTypeVerifier::new(tx),
        }
    }

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

**File:** util/gen-types/src/extension/check_data.rs (L24-28)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
```
