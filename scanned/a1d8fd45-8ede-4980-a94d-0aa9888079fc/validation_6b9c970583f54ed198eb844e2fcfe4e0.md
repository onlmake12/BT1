### Title
`ScriptHashTypeVerifier` Only Validates Lock Script `hash_type`, Silently Skips Type Script — (`verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates the `hash_type` field of each output's **lock script** against the consensus-permitted set (`ENABLED_SCRIPT_HASH_TYPE`). It never inspects the **type script's** `hash_type`. This is a direct structural analog to the reported bug: one variant of a two-variant field (lock vs. type script) is handled; the other is silently skipped.

---

### Finding Description

`ENABLED_SCRIPT_HASH_TYPE` in `util/constant/src/consensus.rs` defines the permitted hash-type byte values:

```
{0 = Data, 1 = Type, 2 = Data1, 4 = Data2}
```

Future data-version hash types such as `Data3` (byte value `6`), `Data4` (`8`), … are syntactically valid `ScriptHashType` values (they pass the molecule-level `check_data()` bit-pattern check) but are **not** in the enabled set.

`ScriptHashTypeVerifier::verify()` enforces the enabled-set constraint only for the lock script:

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
``` [1](#0-0) 

The type script (`output.type_()`) is never examined. A transaction output whose lock script carries `hash_type = Data` (permitted) but whose type script carries `hash_type = Data3` (byte `6`, not permitted) passes this verifier without error.

The verifier's own doc-comment acknowledges the gap: it says "Check whether output **lock** hash type within enabled range." [2](#0-1) 

`NonContextualTransactionVerifier` composes `ScriptHashTypeVerifier` as its sole hash-type gate: [3](#0-2) 

This non-contextual verifier is the first line of defense in both the tx-pool admission path and the block-processing path: [4](#0-3) [5](#0-4) 

---

### Impact Explanation

A transaction with a non-permitted type script `hash_type` (e.g., `Data3 = 6`):

1. **Passes** `ScriptHashTypeVerifier` — the lock script is valid, the type script is never checked.
2. **Enters the tx pool** and is queued for full contextual verification.
3. **Fails** only inside `ContextualTransactionVerifier` → `ScriptVerifier` → `select_version`, which returns `Err(ScriptError::InvalidVmVersion(3))` for `Data3`. [6](#0-5) 

4. The rejection error (`ScriptError::InvalidVmVersion`) is **not** classified as `is_malformed_tx()`: [7](#0-6) 

   Only `ScriptHashTypeNotPermitted` and `InvalidScriptHashType` are malformed-tx errors. Because the type script check never fires, the peer is **never banned** for submitting such transactions.

5. The peer banning gate in the tx-pool service only triggers on malformed-tx errors from non-contextual verification: [8](#0-7) 

**Net effect**: Any RPC caller or relayed-tx submitter can craft transactions with a syntactically valid but consensus-disabled type script `hash_type`. Each such transaction bypasses the cheap non-contextual gate, consumes expensive script-verification cycles, and the submitter is never penalized. This is a resource-exhaustion vector bounded only by the minimum fee rate.

---

### Likelihood Explanation

The entry path is fully unprivileged: the `send_transaction` RPC endpoint accepts transactions from any caller. Crafting a transaction with `hash_type = 6` in a type script requires no special knowledge or access. The molecule-level `check_data()` accepts byte value `6` as a valid `ScriptHashType` bit pattern: [9](#0-8) 

The `ENABLED_SCRIPT_HASH_TYPE` constant makes the gap explicit — `6` is absent from the set: [10](#0-9) 

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's `hash_type` when a type script is present:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock-script check
        check_hash_type(output.lock().hash_type())?;

        // missing type-script check
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}
```

This mirrors the existing molecule-level `CellOutputReader::check_data()` which already validates both lock and type scripts symmetrically.

---

### Proof of Concept

1. Build a `CellOutput` whose lock script uses `hash_type = Data` (byte `0`, permitted) and whose type script uses `hash_type = Data3` (byte `6`, not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Wrap it in a transaction and call `ScriptHashTypeVerifier::new(&tx).verify()`.
3. Observe: the verifier returns `Ok(())` — the non-permitted type script hash type is not caught.
4. Submit the transaction via `send_transaction` RPC; it enters the pool, triggers full script verification, fails with `InvalidVmVersion(3)`, and the submitting peer is not banned.

The existing test `test_not_enabled_hash_type_output_lock` confirms the lock-script path is tested; no corresponding test exists for the type-script path, confirming the gap. [11](#0-10)

### Citations

**File:** verification/src/transaction_verifier.rs (L71-101)
```rust
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

**File:** tx-pool/src/util.rs (L56-83)
```rust
pub(crate) fn non_contextual_verify(
    consensus: &Consensus,
    tx: &TransactionView,
) -> Result<(), Reject> {
    NonContextualTransactionVerifier::new(tx, consensus)
        .verify()
        .map_err(Reject::Verification)?;

    // The ckb consensus does not limit the size of a single transaction,
    // but if the size of the transaction is close to the limit of the block,
    // it may cause the transaction to fail to be packed
    let tx_size = tx.data().serialized_size_in_block() as u64;
    if tx_size > TRANSACTION_SIZE_LIMIT {
        return Err(Reject::ExceededTransactionSizeLimit(
            tx_size,
            TRANSACTION_SIZE_LIMIT,
        ));
    }
    // cellbase is only valid in a block, not as a loose transaction
    if tx.is_cellbase() {
        return Err(Reject::Malformed(
            "cellbase like".to_owned(),
            Default::default(),
        ));
    }

    Ok(())
}
```

**File:** chain/src/chain_service.rs (L72-89)
```rust
    fn non_contextual_verify(&self, block: &BlockView) -> Result<(), Error> {
        let consensus = self.shared.consensus();
        BlockVerifier::new(consensus).verify(block).map_err(|e| {
            debug!("[process_block] BlockVerifier error {:?}", e);
            e
        })?;

        NonContextualBlockTxsVerifier::new(consensus)
            .verify(block)
            .map_err(|e| {
                debug!(
                    "[process_block] NonContextualBlockTxsVerifier error {:?}",
                    e
                );
                e
            })
            .map(|_| ())
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

**File:** util/types/src/core/error.rs (L242-264)
```rust
impl TransactionError {
    /// Returns whether this transaction error indicates that the transaction is malformed.
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

            TransactionError::Immature { .. }
            | TransactionError::CellbaseImmaturity { .. }
            | TransactionError::MismatchedVersion { .. }
            | TransactionError::Compatible { .. }
            | TransactionError::DaoLockSizeMismatch { .. }
            | TransactionError::Internal { .. } => false,
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

**File:** util/gen-types/src/extension/check_data.rs (L10-28)
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

**File:** verification/src/tests/transaction_verifier.rs (L100-122)
```rust
#[test]
pub fn test_not_enabled_hash_type_output_lock() {
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .lock(
                    Script::default()
                        .as_builder()
                        .hash_type(ScriptHashType::Data3)
                        .build(),
                )
                .build(),
        )
        .build();
    let verifier = ScriptHashTypeVerifier::new(&transaction);

    assert_error_eq!(
        verifier.verify().unwrap_err(),
        TransactionError::ScriptHashTypeNotPermitted {
            hash_type: ScriptHashType::Data3.into(),
        },
    );
}
```
