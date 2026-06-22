### Title
`ScriptHashTypeVerifier` Omits Type Script `hash_type` Validation, Allowing Malformed Transactions to Bypass Early Rejection and Peer Banning — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` is the designated non-contextual check that ensures transaction output script `hash_type` fields are within the consensus-permitted range. However, it only validates the **lock script** `hash_type` on each output, completely skipping the **type script** `hash_type`. This is a direct structural analog to the reported vulnerability: a validation function that validates one field (lock script) while silently ignoring another (type script). A transaction sender or P2P relay peer can submit a transaction whose output carries a type script with a disallowed or structurally invalid `hash_type`, bypass the early malformed-transaction check, enter the verify queue, and avoid the peer-ban that would otherwise be applied.

---

### Finding Description

`ScriptHashTypeVerifier` is documented as:

> *"Verify that the ScriptHashType of transaction outputs is within the range permitted by the current consensus rules."*

Its `verify()` implementation iterates over outputs and checks only `output.lock().hash_type()`:

```rust
// verification/src/transaction_verifier.rs  lines 796–814
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

`output.type_()` — the optional type script — is never inspected. A transaction output with a type script whose `hash_type` byte is structurally invalid (e.g., `3`, an odd value ≥ 3) or is a valid-but-not-yet-enabled variant (e.g., `Data3` = `6`) passes `ScriptHashTypeVerifier` without error.

By contrast, the lower-level `check_data` path in `util/gen-types/src/extension/check_data.rs` (used during P2P message parsing) correctly checks **both** lock and type scripts:

```rust
// util/gen-types/src/extension/check_data.rs  lines 24–28
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
```

But `check_data` only tests structural validity (`verify_value`: even or 1), not whether the value is within the **enabled** consensus range. `ScriptHashTypeVerifier` is the only place that enforces the enabled-range constraint, and it misses type scripts entirely.

`NonContextualTransactionVerifier` — the composite non-contextual verifier used by both the tx-pool and block-tx verifier — delegates to `ScriptHashTypeVerifier` as its sole hash-type gate:

```rust
// verification/src/transaction_verifier.rs  lines 94–102
pub fn verify(&self) -> Result<(), Error> {
    self.version.verify()?;
    self.size.verify()?;
    self.empty.verify()?;
    self.duplicate_deps.verify()?;
    self.outputs_data_verifier.verify()?;
    self.script_hash_type.verify()?;   // ← only checks lock script hash_type
    Ok(())
}
```

---

### Impact Explanation

**Peer banning bypass.** When a remote peer relays a transaction, `non_contextual_verify` in `tx-pool/src/process.rs` (lines 318–333) bans the peer if the rejection is `is_malformed_tx()`. `TransactionError::InvalidScriptHashType` and `TransactionError::ScriptHashTypeNotPermitted` are both classified as malformed:

```rust
// util/types/src/core/error.rs  lines 244–255
pub fn is_malformed_tx(&self) -> bool {
    match self {
        TransactionError::InvalidScriptHashType { .. }
        | TransactionError::ScriptHashTypeNotPermitted { .. }
        | ...  => true,
        ...
    }
}
```

Because the type script `hash_type` is never checked by `ScriptHashTypeVerifier`, a transaction with an invalid type script `hash_type` passes non-contextual verification, enters the verify queue, and is only rejected later by the script verifier (`ScriptError::InvalidScriptHashType` from `select_version()`). `ScriptError` is not a `TransactionError`, so `is_malformed_tx()` returns `false`, and the peer is **not banned**. A peer that sends lock-script-invalid transactions is banned; a peer that sends type-script-invalid transactions is not.

**Resource consumption.** Such transactions enter the verify queue and consume CPU cycles in the contextual verifier before being rejected, with no penalty to the sender.

---

### Likelihood Explanation

Any unprivileged RPC caller (`send_transaction`) or P2P relay peer can craft a transaction with a type script whose `hash_type` is set to a disallowed value (e.g., `Data3 = 6`, which is structurally valid per `verify_value` but not in `ENABLED_SCRIPT_HASH_TYPE`). No special privilege, key, or majority hashpower is required. The attacker needs only a valid input cell to construct the transaction.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the `hash_type` of each output's optional type script, mirroring the existing lock-script check:

```rust
// After checking output.lock().hash_type(), also check:
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

Update the struct comment to reflect that both lock and type scripts are checked.

---

### Proof of Concept

1. Construct a transaction output with a valid lock script and a type script whose `hash_type` byte is `6` (`Data3`, structurally valid per `verify_value` but absent from `ENABLED_SCRIPT_HASH_TYPE`).
2. Submit via RPC `send_transaction` or relay via P2P.
3. Observe that `ScriptHashTypeVerifier::verify()` returns `Ok(())` — the transaction passes non-contextual verification.
4. The transaction enters the verify queue and is only rejected later by `select_version()` in `script/src/types.rs` (lines 903–935) with `ScriptError::InvalidScriptHashType`.
5. Confirm the peer is **not** banned (no `ban_malformed` call is triggered), whereas an identical transaction with the same invalid `hash_type` on the **lock** script would cause immediate peer banning.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** verification/src/transaction_verifier.rs (L785-815)
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

**File:** util/gen-types/src/extension/check_data.rs (L24-28)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
```

**File:** tx-pool/src/process.rs (L318-333)
```rust
    pub(crate) async fn non_contextual_verify(
        &self,
        tx: &TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<(), Reject> {
        if let Err(reject) = non_contextual_verify(&self.consensus, tx) {
            if reject.is_malformed_tx()
                && let Some(remote) = remote
            {
                self.ban_malformed(remote.1, format!("reject {reject}"))
                    .await;
            }
            return Err(reject);
        }
        Ok(())
    }
```

**File:** util/types/src/core/error.rs (L244-255)
```rust
    pub fn is_malformed_tx(&self) -> bool {
        match self {
            TransactionError::OutputsSumOverflow { .. }
            | TransactionError::DuplicateCellDeps { .. }
            | TransactionError::DuplicateHeaderDeps { .. }
            | TransactionError::Empty { .. }
            | TransactionError::InsufficientCellCapacity { .. }
            | TransactionError::InvalidSince { .. }
            | TransactionError::ExceededMaximumBlockBytes { .. }
            | TransactionError::InvalidScriptHashType { .. }
            | TransactionError::ScriptHashTypeNotPermitted { .. }
            | TransactionError::OutputsDataLengthMismatch { .. } => true,
```

**File:** script/src/types.rs (L899-936)
```rust
    /// Returns the version of the machine based on the script and the consensus rules.
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
