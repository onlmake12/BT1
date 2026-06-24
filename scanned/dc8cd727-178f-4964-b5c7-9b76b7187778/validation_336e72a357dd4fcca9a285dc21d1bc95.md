Audit Report

## Title
`ScriptHashTypeVerifier` Omits Type-Script `hash_type` Validation, Allowing Disallowed Values to Bypass the Non-Contextual Gate — (File: `verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify` iterates over transaction outputs and validates only `output.lock().hash_type()`, never reading `output.type_()`. A transaction whose output carries a type script with a disallowed or structurally invalid `hash_type` byte passes `NonContextualTransactionVerifier` without error, enters the verify queue, and forces the node to perform cell resolution and full contextual script verification before the transaction is ultimately rejected. The doc-comment on `NonContextualTransactionVerifier` itself confirms the omission, listing only "Check whether output **lock** hash type within enabled range."

## Finding Description
`ScriptHashTypeVerifier::verify` in `verification/src/transaction_verifier.rs` (lines 796–814) reads:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) =
            TryInto::<ScriptHashType>::try_into(output.lock().hash_type())
        { ... } else { ... }
    }
    Ok(())
}
``` [1](#0-0) 

`output.type_()` is never accessed. The consensus-permitted set in `util/constant/src/consensus.rs` contains only `{0, 1, 2, 4}`: [2](#0-1) 

The `ScriptHashType` enum (generated via `seq!` macro for N in 3..=127) defines `Data~N = N << 1`, so value `3` is not a valid discriminant, and value `6` (`Data3`) is a valid discriminant but absent from `ENABLED_SCRIPT_HASH_TYPE`. [3](#0-2) 

A type script with `hash_type = 3` (invalid discriminant) or `hash_type = 6` (`Data3`, valid but not enabled) passes `ScriptHashTypeVerifier` entirely unchecked. The verifier is wired as the first admission gate: [4](#0-3) 

The tx-pool calls `non_contextual_verify` first, then enqueues the transaction for full contextual verification: [5](#0-4) 

`non_contextual_verify` wraps `NonContextualTransactionVerifier::new(tx, consensus).verify()`: [6](#0-5) 

After passing the cheap gate, `verify_rtx` runs `ContextualTransactionVerifier`, which calls `TransactionScriptsVerifier`. Inside `TxInfo::select_version` and `extract_script_and_dep_index`, the type script's `hash_type` is finally checked via `ScriptHashType::try_from`, returning `ScriptError::InvalidScriptHashType` — but only after cell resolution has already been performed: [7](#0-6) [8](#0-7) 

The existing unit tests confirm the asymmetry: `test_unknown_hash_type_output_lock` and `test_not_enabled_hash_type_output_lock` cover lock scripts with `hash_type = 3` and `Data3` respectively, but no corresponding tests exist for type scripts. [9](#0-8) 

## Impact Explanation
This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

The non-contextual verifier is intentionally cheap — it runs before any cell resolution or VM execution. When it passes a transaction, the node enqueues it for full contextual verification, which requires resolving all input cells (database lookups) and running `ContextualTransactionVerifier`. An attacker submitting a stream of transactions with valid lock scripts but disallowed type-script `hash_type` values forces each receiving node to pay the cell-resolution and script-verifier-setup cost for every such transaction before rejection. Because the transactions never pass full verification, they are never mined and the attacker pays no on-chain fee. The attack is repeatable at negligible cost.

## Likelihood Explanation
No privilege is required. Any RPC caller (`send_raw_transaction`) or P2P peer can submit a raw transaction. Setting a single byte (`hash_type`) in a molecule-encoded `Script` struct to a disallowed value (e.g., `3` or `6`) is trivial. The attack is repeatable indefinitely and scales with the number of nodes the attacker connects to directly.

## Recommendation
Extend `ScriptHashTypeVerifier::verify` to also validate the type script of every output, mirroring the existing lock-script check:

```rust
// inside the same loop, after the lock check:
if let Some(type_script) = output.type_().to_opt() {
    if let Ok(hash_type) =
        TryInto::<ScriptHashType>::try_into(type_script.hash_type())
    {
        let val: u8 = hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(
                TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into(),
            );
        }
    } else {
        return Err((TransactionError::InvalidScriptHashType {
            hash_type: type_script.hash_type(),
        })
        .into());
    }
}
```

Update the `NonContextualTransactionVerifier` doc-comment (line 70) to state that both lock and type hash types are checked. [10](#0-9) 

## Proof of Concept
1. Build a `TransactionView` with one output: lock script `hash_type = 0` (Data, always-success code hash), type script `hash_type = 3` (not a valid `ScriptHashType` discriminant).
2. Submit via RPC `send_raw_transaction`.
3. `NonContextualTransactionVerifier::verify` → `ScriptHashTypeVerifier::verify` checks `output.lock().hash_type() == 0` ✓, never reads `output.type_()`, returns `Ok(())`.
4. The transaction enters the verify queue; `verify_rtx` resolves cells and constructs `ContextualTransactionVerifier`.
5. `TransactionScriptsVerifier` calls `select_version` on the type script, `ScriptHashType::try_from(3u8)` fails, returns `ScriptError::InvalidScriptHashType` — after cell resolution cost has been paid.
6. Confirm the asymmetry: replace the type script with a lock script carrying `hash_type = 3`; `ScriptHashTypeVerifier` immediately returns `InvalidScriptHashType` without any cell resolution.

### Citations

**File:** verification/src/transaction_verifier.rs (L61-78)
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

**File:** tx-pool/src/process.rs (L335-352)
```rust
    pub(crate) async fn resumeble_process_tx(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
        self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
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

**File:** script/src/types.rs (L828-861)
```rust
    fn extract_script_and_dep_index(
        &self,
        script: &Script,
    ) -> Result<(&LazyData, &usize), ScriptError> {
        let script_hash_type = ScriptHashType::try_from(script.hash_type())
            .map_err(|err| ScriptError::InvalidScriptHashType(err.to_string()))?;
        match script_hash_type {
            ScriptHashType::Data | ScriptHashType::Data1 | ScriptHashType::Data2 => {
                if let Some((dep_index, lazy)) = self.binaries_by_data_hash.get(&script.code_hash())
                {
                    Ok((lazy, dep_index))
                } else {
                    Err(ScriptError::ScriptNotFound(script.code_hash()))
                }
            }
            ScriptHashType::Type => {
                if let Some(ref bin) = self.binaries_by_type_hash.get(&script.code_hash()) {
                    match bin {
                        Binaries::Unique(_, dep_index, lazy) => Ok((lazy, dep_index)),
                        Binaries::Duplicate(_, dep_index, lazy) => Ok((lazy, dep_index)),
                        Binaries::Multiple => Err(ScriptError::MultipleMatches),
                    }
                } else {
                    Err(ScriptError::ScriptNotFound(script.code_hash()))
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

**File:** verification/src/tests/transaction_verifier.rs (L82-122)
```rust
pub fn test_unknown_hash_type_output_lock() {
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .lock(Script::default().as_builder().hash_type(3).build())
                .build(),
        )
        .build();
    let verifier = ScriptHashTypeVerifier::new(&transaction);

    assert_error_eq!(
        verifier.verify().unwrap_err(),
        TransactionError::InvalidScriptHashType {
            hash_type: 3.into(),
        },
    );
}

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
