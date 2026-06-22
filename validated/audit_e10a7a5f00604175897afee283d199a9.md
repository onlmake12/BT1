### Title
Incomplete `ScriptHashType` Validation in `ScriptHashTypeVerifier` — Output Type Scripts Not Checked Against `ENABLED_SCRIPT_HASH_TYPE` - (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` only validates the `hash_type` of **output lock scripts** against the compile-time whitelist `ENABLED_SCRIPT_HASH_TYPE`, but completely omits the same check for **output type scripts**. This mirrors the original report's vulnerability class: an incomplete classification/whitelist check that fails to cover all relevant inputs, allowing structurally invalid data to pass a gating check. An unprivileged transaction sender can craft a transaction whose output carries a type script with an unactivated `ScriptHashType` (e.g., `Data3` = `6`), bypass non-contextual verification, and inject the transaction into the tx-pool.

---

### Finding Description

`ENABLED_SCRIPT_HASH_TYPE` is a compile-time static set containing only the four currently activated hash type byte values:

```
{ 0 (Data), 1 (Type), 2 (Data1), 4 (Data2) }
``` [1](#0-0) 

The `ScriptHashType` enum, however, is defined with variants up to `Data127` (values `0`, `1`, `2`, `4`, `6`, `8`, …, `254`), all of which parse successfully via `ScriptHashType::try_from()`: [2](#0-1) 

`ScriptHashTypeVerifier::verify()` iterates only over `output.lock().hash_type()` — the lock script of each output — and checks it against `ENABLED_SCRIPT_HASH_TYPE`. It never inspects `output.type_()`: [3](#0-2) 

This verifier is the sole non-contextual gate for hash type enforcement in `NonContextualTransactionVerifier`: [4](#0-3) 

By contrast, `select_version()` and `extract_script_and_dep_index()` — which run only at script execution time — do reject unactivated hash types via a catch-all arm: [5](#0-4) [6](#0-5) 

The gap is that non-contextual verification (which runs at tx-pool admission) does not enforce the whitelist on type scripts, while contextual enforcement (script execution) runs later and only during block verification.

---

### Impact Explanation

An attacker submits a transaction whose output has a type script with `hash_type = 6` (`Data3`, a valid enum variant but absent from `ENABLED_SCRIPT_HASH_TYPE`). `ScriptHashTypeVerifier` passes it because it only checks lock scripts. The transaction enters the tx-pool. When the miner assembles a block and script execution runs, `select_version` returns `InvalidScriptHashType` and the transaction is rejected — but only after consuming tx-pool resources and potentially being included in a block template that then fails peer verification. This enables:

- **Tx-pool pollution**: An unprivileged sender can flood the pool with structurally invalid transactions that pass the non-contextual gate.
- **Miner resource waste**: If the invalid transaction reaches block assembly before contextual rejection, the miner's block template is invalidated.
- **Defense-in-depth failure**: The non-contextual check is supposed to be a complete structural gate; its incompleteness means the system relies entirely on the later, more expensive script execution path to catch this class of error.

---

### Likelihood Explanation

Any unprivileged transaction sender reachable via the RPC (`send_transaction`) or P2P relay path can craft such a transaction. No special privileges, keys, or majority hashpower are required. The `ScriptHashType` enum exposes `Data3` through `Data127` as valid parseable values, and the RPC/P2P layer accepts any structurally valid molecule-encoded transaction. The attack is trivially constructable.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also check the `hash_type` of each output's type script (when present) against `ENABLED_SCRIPT_HASH_TYPE`, mirroring the existing lock script check:

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

This makes the non-contextual gate complete and consistent with the intent stated in the comment: *"Verify that the ScriptHashType of transaction outputs is within the range permitted by the current consensus rules."* [7](#0-6) 

---

### Proof of Concept

1. Construct a transaction with one output whose lock script uses `ScriptHashType::Data` (value `0`, valid) and whose type script uses `ScriptHashType::Data3` (value `6`, not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Submit via `send_transaction` RPC.
3. `NonContextualTransactionVerifier` runs `ScriptHashTypeVerifier::verify()`, which iterates only over lock scripts — the lock is `Data` (valid), so it passes.
4. The transaction is admitted to the tx-pool.
5. During block assembly, `select_version` is called for the type script group, hits the catch-all arm, and returns `InvalidScriptHashType` — the transaction is rejected only at this late stage.

The test at `verification/src/tests/transaction_verifier.rs:100-122` confirms that `Data3` in a **lock** script is caught, but no analogous test exists for `Data3` in a **type** script, confirming the gap. [8](#0-7)

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

**File:** script/src/types.rs (L854-860)
```rust
            hash_type => {
                return Err(ScriptError::InvalidScriptHashType(format!(
                    "The ScriptHashType/{:?} has not been activated, and is not permitted for use.",
                    hash_type
                )));
            }
        }
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
