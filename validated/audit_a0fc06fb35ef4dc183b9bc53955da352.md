### Title
`ScriptHashTypeVerifier` Omits Type Script Hash-Type Validation, Allowing Invalid Transactions into the Tx-Pool — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` enforces that output lock scripts use a hash type within `ENABLED_SCRIPT_HASH_TYPE`, but performs **no equivalent check on output type scripts**. A transaction sender can submit a transaction whose output carries a type script with a non-permitted `hash_type` (e.g., `Data3` = 6), bypass the non-contextual gate, and have the transaction admitted to the tx-pool. The transaction will later fail during contextual script execution, but only after the node has already accepted, stored, and relayed it.

---

### Finding Description

`ENABLED_SCRIPT_HASH_TYPE` is the consensus-level set of permitted hash-type values: [1](#0-0) 

```
{0 = Data, 1 = Type, 2 = Data1, 4 = Data2}
```

`ScriptHashTypeVerifier::verify()` iterates over every transaction output and validates the **lock** script's `hash_type` against this set: [2](#0-1) 

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) { ... }
        } else { ... }
    }
    Ok(())
}
```

The **type script** (`output.type_()`) is never inspected. The struct's own documentation confirms the omission: [3](#0-2) 

> "Check whether output **lock** hash type within enabled range"

By contrast, the lower-level `check_data()` helper does check both scripts, but only for structural byte-validity (any even value or `1`), not for the narrower `ENABLED_SCRIPT_HASH_TYPE` set: [4](#0-3) 

```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
```

`check_data()` accepts `hash_type = 6` (`Data3`) as structurally valid (6 is even), but `ENABLED_SCRIPT_HASH_TYPE` does not contain 6. `ScriptHashTypeVerifier` never calls `check_data()` on the type script, so the gap is never closed at the non-contextual layer.

`ScriptHashTypeVerifier` is composed into `NonContextualTransactionVerifier`, which is the gate used by the tx-pool: [5](#0-4) [6](#0-5) 

---

### Impact Explanation

A transaction output with `type_().hash_type() = 6` (`Data3`, not in `ENABLED_SCRIPT_HASH_TYPE`) passes `NonContextualTransactionVerifier` and is admitted to the tx-pool. The node stores it, propagates it to peers, and the block assembler may attempt to include it. The transaction ultimately fails during contextual script execution (`select_version` returns `InvalidScriptHashType`): [7](#0-6) 

This means:
- **Tx-pool pollution**: Invalid transactions occupy pool slots and consume relay bandwidth.
- **Wasted block-assembly cycles**: The assembler may attempt to include the transaction before evicting it.
- **Inconsistent enforcement**: The same consensus rule (`ENABLED_SCRIPT_HASH_TYPE`) is enforced for lock scripts at the non-contextual layer but deferred to script execution for type scripts, creating an asymmetric and exploitable gap.

---

### Likelihood Explanation

Any unprivileged RPC caller (`send_transaction`) or P2P transaction relayer can craft such a transaction. The only cost is a valid input cell to pay fees. The attacker does not need any special privilege, key, or majority hash power. The `ScriptHashType` enum already defines `Data3 = 6` as a recognized (but not yet enabled) value: [8](#0-7) 

so constructing such a transaction requires no reverse engineering.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's `hash_type` when a type script is present:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock check
        check_hash_type(output.lock().hash_type())?;
        // add type script check
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}
```

This mirrors the pattern already used in `CellOutputReader::check_data()`, which validates both `lock` and `type_` fields symmetrically. [4](#0-3) 

---

### Proof of Concept

1. Obtain a live cell with sufficient CKB to pay fees.
2. Construct a transaction whose output has:
   - A valid lock script (`hash_type = 1`, `Type`)
   - A type script with `hash_type = 6` (`Data3`, not in `ENABLED_SCRIPT_HASH_TYPE`)
3. Submit via `send_transaction` RPC.
4. Observe: `NonContextualTransactionVerifier` passes (lock hash type is valid; type script hash type is never checked).
5. Observe: transaction enters the tx-pool and is relayed.
6. Observe: transaction fails during script execution with `InvalidScriptHashType` or `ScriptHashTypeNotPermitted` only when contextual verification runs.

The existing test suite confirms that `ScriptHashTypeVerifier` only tests lock scripts and has no test for an invalid type script hash type: [9](#0-8)

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

**File:** util/gen-types/src/extension/check_data.rs (L24-27)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
```

**File:** tx-pool/src/util.rs (L1-5)
```rust
use crate::error::Reject;
use crate::pool::TxPool;
use ckb_chain_spec::consensus::Consensus;
use ckb_dao::DaoCalculator;
use ckb_script::ChunkCommand;
```

**File:** script/src/types.rs (L930-936)
```rust
            hash_type => {
                return Err(ScriptError::InvalidScriptHashType(format!(
                    "The ScriptHashType/{:?} has not been activated, and is not permitted for use.",
                    hash_type
                )));
            }
        }
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
