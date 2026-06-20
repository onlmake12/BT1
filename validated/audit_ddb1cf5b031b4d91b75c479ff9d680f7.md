### Title
Incomplete `ScriptHashTypeVerifier` Checks Only Lock Script Hash Type, Skips Type Script — (`File: verification/src/transaction_verifier.rs`)

### Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates the `hash_type` field of each output's **lock script** against `ENABLED_SCRIPT_HASH_TYPE`, but entirely omits the same check for each output's **type script**. This is a direct analog to the `verifyState` incomplete-check pattern: a structure with two fields (lock script, type script) is only partially validated, allowing the unchecked field to carry a non-permitted value past the non-contextual gate.

### Finding Description

`ScriptHashTypeVerifier::verify()` in `verification/src/transaction_verifier.rs` (lines 796–814) loops over `self.transaction.outputs()` and for each output calls `output.lock().hash_type()` — the lock script — to test membership in `ENABLED_SCRIPT_HASH_TYPE`. The type script (`output.type_()`) is never inspected. [1](#0-0) 

The comment on `NonContextualTransactionVerifier` itself documents the gap: it says "Check whether output lock hash type within enabled range" — the type script is not mentioned. [2](#0-1) 

The lower-level `check_data` function does visit both lock and type scripts, but it only validates that the raw byte is a structurally valid `ScriptHashType` value (even number or `1`). It does **not** check whether the hash type is in the currently-enabled set. [3](#0-2) [4](#0-3) 

`ENABLED_SCRIPT_HASH_TYPE` is a strict subset of structurally-valid values. For example, `Data3` (byte value `6`) is structurally valid (even number, passes `check_data`) but is not in the enabled set (confirmed by the existing test `test_not_enabled_hash_type_output_lock`). [5](#0-4) 

A transaction output carrying a type script with `hash_type = Data3` (or any future/disabled VM version) therefore passes `ScriptHashTypeVerifier` and is admitted to the tx pool. The error is only caught later, inside `select_version`, when the script group is actually executed. [6](#0-5) 

### Impact Explanation

Any unprivileged RPC caller or P2P relay peer can craft a transaction whose outputs contain a type script with a non-permitted `hash_type` (e.g., `Data3 = 6`, `Data4 = 8`, …). Such a transaction:

1. Passes `check_data` (structurally valid byte).
2. Passes `ScriptHashTypeVerifier` (lock script is valid; type script is never checked).
3. Enters the tx pool and is relayed to peers.
4. Fails only at script-execution time, after consuming tx-pool memory, relay bandwidth, and partial verification CPU on every node that receives it.

Because the relay protocol propagates transactions to all connected peers before execution, a single crafted transaction can pollute the mempools of many nodes simultaneously. The tx pool has size limits, so sustained injection of such transactions can displace legitimate transactions (mempool eviction pressure).

### Likelihood Explanation

The entry path is the standard `send_transaction` RPC or the P2P relay protocol — both are reachable by any unprivileged user. No key, no miner role, no Sybil attack is required. The crafted transaction is trivially constructed: take any valid transaction and set the type script's `hash_type` byte to `6` (Data3). The gap has existed since `ScriptHashTypeVerifier` was introduced and is not guarded by any rate-limit specific to this case.

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also check `output.type_()` when present:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock script check
        check_hash_type(output.lock().hash_type())?;
        // add type script check
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}
```

where `check_hash_type` encapsulates the existing `TryInto<ScriptHashType>` + `ENABLED_SCRIPT_HASH_TYPE.contains` logic. Update the doc-comment on `NonContextualTransactionVerifier` to reflect that both lock and type script hash types are checked.

### Proof of Concept

```
// Attacker submits via RPC send_transaction:
// output = CellOutput {
//   capacity: ...,
//   lock: Script { hash_type: 0 (Data), ... },   // valid, passes check
//   type: Some(Script { hash_type: 6 (Data3), ... }) // NOT checked by ScriptHashTypeVerifier
// }
//
// Result: transaction admitted to tx pool, relayed to peers,
// fails only at script execution with InvalidVmVersion(3).
``` [1](#0-0) [7](#0-6)

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

**File:** util/gen-types/src/extension/check_data.rs (L24-28)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
```

**File:** util/gen-types/src/core.rs (L39-41)
```rust
    pub fn verify_value(v: u8) -> bool {
        v.is_multiple_of(2) || v == 1
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

**File:** util/constant/src/consensus.rs (L1-5)
```rust
use phf::{Set, phf_set};

/// Dampening factor.
pub const TAU: u64 = 2;

```
