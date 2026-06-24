Audit Report

## Title
`ScriptHashTypeVerifier` Omits Type Script Hash-Type Enforcement, Enabling Tx-Pool Bypass and Consensus Deviation — (`verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` iterates over transaction outputs but checks only the lock script's `hash_type`, never the type script's `hash_type`. Any transaction whose outputs carry a type script with a non-permitted hash type (e.g., `Data3 = 6`) silently passes the non-contextual gate, enters the tx pool, and forces a full contextual verification cycle before eviction. Under `assume_valid_target` (which sets `Switch::DISABLE_SCRIPT`), the non-contextual check is the sole enforcement gate, and such a transaction is committed to the local chain, diverging from fully-verifying peers.

## Finding Description

**Root cause — lock-only check in `ScriptHashTypeVerifier::verify()`:**

`verification/src/transaction_verifier.rs` lines 796–814 iterate over outputs and call `output.lock().hash_type()` exclusively:

```rust
for output in self.transaction.outputs() {
    if let Ok(hash_type) =
        TryInto::<ScriptHashType>::try_into(output.lock().hash_type())  // lock only
    { ... }
}
```

`output.type_()` is never read. A type script with `hash_type = 6` (`Data3`) passes this loop without any check.

**`ScriptHashType` enum includes `Data3 = 6` and beyond:**

`util/gen-types/src/core.rs` lines 9–32 use a `seq!` macro to generate variants `DataN = N << 1` for N in 3..=127. `Data3 = 6` is a valid enum variant; `TryInto::<ScriptHashType>::try_into(6u8)` succeeds (it does not hit the `Err` branch), and `verify_value(6)` returns `true` (6 is even). However, `ENABLED_SCRIPT_HASH_TYPE` (`util/constant/src/consensus.rs` lines 7–12) contains only `{0, 1, 2, 4}`, so `val = 6` would fail the `contains` check — but only if the type script were ever examined.

**Tx-pool admission path:**

`tx-pool/src/util.rs` lines 56–82 call `NonContextualTransactionVerifier::new(tx, consensus).verify()`, which invokes `ScriptHashTypeVerifier::verify()`. A transaction with lock `hash_type = 0` and type `hash_type = 6` passes this gate and enters the pool. `verify_rtx()` later calls `select_version()` (`script/src/types.rs` lines 900–937), where the catch-all arm at line 930 returns `ScriptError::InvalidScriptHashType` for `Data3`, evicting the transaction — but only after consuming contextual verification resources.

**Consensus deviation under `assume_valid_target` / `DISABLE_SCRIPT`:**

`Switch::DISABLE_SCRIPT` (`verification/traits/src/lib.rs` line 41, bit `0b01000000`) is set by the `assume_valid_target` sync optimisation (`chain/src/verify.rs`, `sync/src/synchronizer/mod.rs`). When active, `select_version()` is never reached. The non-contextual check (`ScriptHashTypeVerifier`) is the only enforcement gate, and it does not cover type scripts. A block containing a transaction with a non-permitted type-script hash type is accepted by the syncing node but rejected by fully-verifying peers, producing a local chain state inconsistent with consensus.

## Impact Explanation

**Primary (High — network congestion with few costs):** Any unprivileged RPC caller or P2P relay peer can craft transactions with a valid lock script and a type script carrying `hash_type = 6`. Each such transaction passes `non_contextual_verify()` unconditionally, enters the pool, and forces a contextual verification cycle. Because the non-contextual gate is the intended cheap admission filter, this bypass allows an attacker to saturate the contextual verification pipeline at minimal cost, matching the "bad designs which could cause CKB network congestion with few costs" High impact class.

**Secondary (Critical — consensus deviation):** Under `assume_valid_target` (a production-reachable configuration), a malicious block relayer can deliver a block whose transactions carry non-permitted type-script hash types. The syncing node accepts the block (no script execution, `ScriptHashTypeVerifier` misses type scripts), while fully-verifying peers reject it, causing a consensus split. This matches the "could easily cause consensus deviation" Critical impact class.

## Likelihood Explanation

The tx-pool exhaustion path requires no privilege and no special node configuration: any `send_transaction` RPC caller or P2P relay peer can trigger it deterministically on every submission. Constructing the malicious transaction requires changing a single field (`type_script.hash_type`) in an otherwise valid transaction. The consensus deviation path additionally requires the target node to be running with `assume_valid_target` enabled, which is a documented production sync optimisation, not a test-only flag.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type for every output:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        check_hash_type(output.lock().hash_type())?;
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}
```

Update the `NonContextualTransactionVerifier` doc comment at line 70 from "Check whether output lock hash type within enabled range" to "Check whether output lock and type script hash types are within the enabled range."

## Proof of Concept

1. Build a `TransactionView` with one output: lock script `hash_type = 0` (Data), type script `hash_type = 6` (Data3).
2. Submit via `send_transaction` RPC or P2P relay.
3. `non_contextual_verify()` → `ScriptHashTypeVerifier::verify()` checks `output.lock().hash_type() = 0` → passes; type script is never examined → passes.
4. Transaction enters the tx pool.
5. `verify_rtx()` → `select_version()` hits the catch-all arm for `Data3` → `ScriptError::InvalidScriptHashType` → transaction evicted.
6. Repeat at high rate to saturate contextual verification.
7. For consensus deviation: configure a node with `assume_valid_target`, relay a block containing the above transaction; the node commits the block while fully-verifying peers reject it.

**Relevant code locations:**
- [1](#0-0) 
- [2](#0-1) 
- [3](#0-2) 
- [4](#0-3) 
- [5](#0-4) 
- [6](#0-5) 
- [7](#0-6)

### Citations

**File:** verification/src/transaction_verifier.rs (L61-103)
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

**File:** util/gen-types/src/core.rs (L9-33)
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

**File:** verification/traits/src/lib.rs (L40-42)
```rust
        /// Disable script verification
        const DISABLE_SCRIPT            = 0b01000000;

```
