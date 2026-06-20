### Title
Missing Type Script Hash Type Validation in `ScriptHashTypeVerifier` Allows Peer Banning Bypass and Resource Exhaustion - (File: verification/src/transaction_verifier.rs)

### Summary

`ScriptHashTypeVerifier` validates only the **lock script** hash type of transaction outputs against the `ENABLED_SCRIPT_HASH_TYPE` set, but entirely omits the same check for **type scripts**. An unprivileged peer can craft transactions whose output type scripts carry a syntactically valid but consensus-disabled hash type (e.g., `Data3` = 6). These transactions pass the non-contextual malformed-transaction gate, consume expensive script-execution resources, and — critically — never trigger peer banning, because the rejection path that sets `is_malformed_tx = true` is never reached.

### Finding Description

`ENABLED_SCRIPT_HASH_TYPE` is defined as the static set `{0, 1, 2, 4}` (Data, Type, Data1, Data2): [1](#0-0) 

The `ScriptHashType` enum extends to `Data3` (6), `Data4` (8), … `Data127` (254) via a macro expansion. All even values pass the structural `verify_value` check: [2](#0-1) 

The low-level `check_data()` on `CellOutputReader` validates both lock and type scripts for structural correctness (even-or-1 rule), so `Data3` (6) passes it: [3](#0-2) 

`ScriptHashTypeVerifier::verify()` iterates over outputs and checks only `output.lock().hash_type()` against `ENABLED_SCRIPT_HASH_TYPE`. There is no corresponding check for `output.type_().hash_type()`: [4](#0-3) 

`ScriptHashTypeVerifier` is wired into `NonContextualTransactionVerifier`, which is the gate that decides whether a peer is banned: [5](#0-4) 

In the tx-pool, `non_contextual_verify` bans the remote peer **only** when `reject.is_malformed_tx()` is true: [6](#0-5) 

`is_malformed_tx()` returns `true` for `InvalidScriptHashType` and `ScriptHashTypeNotPermitted` — errors that `ScriptHashTypeVerifier` emits for lock scripts — but a transaction whose type script carries `Data3` never triggers either of those errors at the non-contextual stage: [7](#0-6) 

Instead, the transaction proceeds to full script execution, where `select_version()` returns `Err(ScriptError::InvalidVmVersion(3))`: [8](#0-7) 

That error is not classified as a malformed-transaction error, so the peer is never banned.

### Impact Explanation

1. **Peer banning bypass**: A remote peer can send an unbounded stream of transactions whose output type scripts use `Data3`–`Data127` hash types. Each transaction passes the non-contextual gate, enters the verify queue, and triggers a full (potentially multi-script) execution run before being rejected. Because the rejection never sets `is_malformed_tx`, the peer is never disconnected or banned.

2. **Resource exhaustion**: Script execution is the most CPU-intensive step in transaction validation. Forcing the node to run it for every crafted transaction — without any banning consequence — constitutes a sustained, low-cost denial-of-service against the verify pipeline.

3. **Asymmetric treatment**: Lock scripts with `Data3` are caught and the peer is banned. Type scripts with `Data3` are not caught and the peer is not banned. This inconsistency is the direct root cause.

### Likelihood Explanation

Crafting such a transaction requires no special privilege: any RPC caller or P2P peer can submit a transaction with a type script whose `hash_type` byte is set to 6 (or any even value ≥ 6). No key material, mining power, or social engineering is needed. The attack is repeatable at negligible cost because no fees are deducted for rejected transactions.

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also iterate over each output's optional type script and apply the same `ENABLED_SCRIPT_HASH_TYPE` membership check:

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

        // NEW: type script check
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

This makes the rejection path emit `ScriptHashTypeNotPermitted`, which `is_malformed_tx()` already recognises, restoring peer banning for type-script hash type abuse and eliminating the unnecessary script-execution overhead.

### Proof of Concept

1. Construct a transaction with one output:
   - Lock script: `hash_type = 0` (Data) — passes all checks.
   - Type script: `hash_type = 6` (Data3) — syntactically valid (even number), not in `ENABLED_SCRIPT_HASH_TYPE`.
2. Submit via `send_transaction` RPC or relay over P2P.
3. Observe: `ScriptHashTypeVerifier` passes (only checks lock). The transaction enters the verify queue.
4. Script execution runs, returns `InvalidVmVersion(3)`, transaction is rejected.
5. Observe: the submitting peer is **not** banned. Repeat from step 2 indefinitely.

The test `test_not_enabled_hash_type_output_lock` in `verification/src/tests/transaction_verifier.rs` demonstrates the existing lock-script check works correctly, but no equivalent test exists for type scripts, confirming the gap: [9](#0-8)

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

**File:** util/gen-types/src/core.rs (L9-42)
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
});

impl ScriptHashType {
    /// when the low 1 bit is 1, it indicates the type
    /// when the low 1 bit is 0, it indicates the data
    #[inline]
    pub fn verify_value(v: u8) -> bool {
        v.is_multiple_of(2) || v == 1
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

**File:** util/types/src/core/error.rs (L242-265)
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
}
```

**File:** script/src/types.rs (L900-937)
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
    }
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
