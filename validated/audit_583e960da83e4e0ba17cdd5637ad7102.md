Audit Report

## Title
`ScriptHashTypeVerifier` Omits `hash_type` Validation for `type_` Scripts in Transaction Outputs — (File: `verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates the `hash_type` of each output's `lock` script against `ENABLED_SCRIPT_HASH_TYPE`, but performs no equivalent check on each output's `type_` script. A transaction whose output carries a `type_` script with an unsupported or unknown `hash_type` passes the non-contextual gate and is admitted to the tx-pool, bypassing the cheap pre-filter that exists precisely to avoid this. Scenario B (silent type-script bypass) is not valid — the VM's `extract_script_and_dep_index` and `select_version` both return a hard `InvalidScriptHashType` error for unknown hash types rather than silently skipping execution. The concrete impact is Scenario A: tx-pool pollution with transactions that will always fail contextual verification.

## Finding Description
`ScriptHashTypeVerifier::verify()` at lines 796–814 of `verification/src/transaction_verifier.rs` loops over outputs and applies the `ENABLED_SCRIPT_HASH_TYPE` guard exclusively to `output.lock().hash_type()`:

```rust
for output in self.transaction.outputs() {
    if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
        let val: u8 = hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) { ... }
    } else { ... }
    // output.type_() is never inspected
}
``` [1](#0-0) 

`ENABLED_SCRIPT_HASH_TYPE` permits only `{0, 1, 2, 4}` (Data, Type, Data1, Data2). [2](#0-1) 

`output.type_()` is an optional `Script` with its own `hash_type` field; no analogous check exists for it anywhere in `ScriptHashTypeVerifier`.

The exploit path is:

1. Attacker calls `send_transaction` RPC with a transaction whose output `type_` script carries `hash_type = 6` (Data3) or any raw byte not in `{0,1,2,4}`.
2. `non_contextual_verify` in `tx-pool/src/util.rs` calls `NonContextualTransactionVerifier::verify()` → `ScriptHashTypeVerifier::verify()`, which returns `Ok(())` without inspecting `output.type_().hash_type()`. [3](#0-2) 
3. The transaction is admitted to the tx-pool.
4. At contextual verification, `TxInfo::extract_script_and_dep_index` calls `ScriptHashType::try_from(script.hash_type())` and returns `ScriptError::InvalidScriptHashType` for the unsupported value — the transaction is then rejected. [4](#0-3) 

The same omission is inherited by `NonContextualBlockTxsVerifier`, which calls `NonContextualTransactionVerifier::verify()` for every block transaction. [5](#0-4) 

**Why Scenario B (type-script bypass) is invalid:** `select_version` and `extract_script_and_dep_index` both contain exhaustive `match` arms that return a hard error for any `ScriptHashType` variant outside the four known ones; execution is never silently skipped. [6](#0-5) 

## Impact Explanation
The concrete impact is **tx-pool pollution**: an unprivileged attacker can repeatedly submit zero-value-output transactions whose `type_` script carries an unsupported `hash_type`. Each such transaction passes the cheap non-contextual gate, occupies a slot in the tx-pool, and forces the node to perform input resolution and the initial stages of contextual verification before the `InvalidScriptHashType` error is raised. At scale this wastes memory and I/O on transactions that are guaranteed to be rejected. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**, though the per-transaction contextual cost is low (no VM execution reaches the point of cycle consumption), which tempers the severity toward the lower end of that band.

## Likelihood Explanation
The entry path requires only an RPC call to `send_transaction` or a P2P relay submission — no privileged access, no key material, no majority hashpower. Crafting a transaction with an output whose `type_` script has an unsupported `hash_type` byte is trivial. The attacker must pay the minimum relay fee per transaction, so the attack is not entirely free, but the cost-per-rejection is low because the contextual failure occurs before any VM cycles are consumed. The omission is present in the current production code path for both tx-pool admission and block-level non-contextual verification.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to apply the same `ENABLED_SCRIPT_HASH_TYPE` guard to `output.type_()` when it is present:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock check (unchanged)
        check_hash_type(output.lock().hash_type())?;
        // add type_ check
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}
```

Extract the repeated guard into a private helper to prevent future divergence between the two checks. Add a companion unit test mirroring `test_not_enabled_hash_type_output_lock` but targeting the `type_` script field. [7](#0-6) 

## Proof of Concept
The existing test `test_not_enabled_hash_type_output_lock` at line 101 of `verification/src/tests/transaction_verifier.rs` confirms that a `lock` script with `hash_type = Data3` (raw byte 6) is correctly rejected. [7](#0-6) 

An analogous test for `type_` scripts does not exist. The following test would currently **pass** (i.e., `verify()` returns `Ok(())`), confirming the omission:

```rust
#[test]
pub fn test_not_enabled_hash_type_output_type_script() {
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .type_(Some(
                    Script::default()
                        .as_builder()
                        .hash_type(ScriptHashType::Data3)
                        .build(),
                ).pack())
                .build(),
        )
        .build();
    let verifier = ScriptHashTypeVerifier::new(&transaction);
    // Currently returns Ok(()), demonstrating the omission:
    assert!(verifier.verify().is_ok());
}
```

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

**File:** script/src/types.rs (L832-860)
```rust
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

**File:** verification/src/block_verifier.rs (L273-286)
```rust
impl<'a> NonContextualBlockTxsVerifier<'a> {
    /// Creates a new NonContextualBlockTxsVerifier
    pub fn new(consensus: &'a Consensus) -> Self {
        NonContextualBlockTxsVerifier { consensus }
    }

    /// Perform context-independent verification checks for block transactions
    pub fn verify(&self, block: &BlockView) -> Result<Vec<()>, Error> {
        block
            .transactions()
            .iter()
            .map(|tx| NonContextualTransactionVerifier::new(tx, self.consensus).verify())
            .collect()
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
