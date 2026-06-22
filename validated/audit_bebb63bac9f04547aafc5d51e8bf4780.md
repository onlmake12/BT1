### Title
`ScriptHashTypeVerifier` Only Validates Lock Script Hash Types, Silently Skipping Type Script Hash Types — (File: `verification/src/transaction_verifier.rs`)

### Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and enforces the `ENABLED_SCRIPT_HASH_TYPE` consensus rule only for each output's **lock script**. The optional **type script** of every output is never checked. This is a direct structural analog to the reported bug: a multi-step validation pipeline handles one variant of a two-variant structure (lock vs. type) and silently skips the other.

### Finding Description

`ScriptHashTypeVerifier` is the non-contextual gatekeeper that is supposed to reject any transaction whose outputs reference a `ScriptHashType` value outside the currently-enabled set (e.g., `Data3`/value 6 and above are not enabled). Its `verify()` method reads:

```rust
// verification/src/transaction_verifier.rs  lines 796-814
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

The loop body calls `output.lock().hash_type()` and checks it against `ENABLED_SCRIPT_HASH_TYPE`. It never calls `output.type_().to_opt()` and never checks the type script's hash type. Every `CellOutput` has exactly two script slots — lock (mandatory) and type (optional) — and the verifier only covers one of them.

The `ScriptHashType` enum is defined with variants `Data`(0), `Type`(1), `Data1`(2), `Data2`(4), `Data3`(6) … `Data127`(254): [2](#0-1) 

`ENABLED_SCRIPT_HASH_TYPE` (defined in `util/constant/src/consensus.rs`) restricts the permitted set. Values such as `Data3`(6) are structurally valid bit patterns (they pass `ScriptHashType::verify_value`) but are not in the enabled set.

The downstream `select_version()` in `script/src/types.rs` does handle the full set of variants and returns `Err(ScriptError::InvalidScriptHashType(...))` for any hash type that is not `Data`, `Data1`, `Data2`, or `Type`: [3](#0-2) 

So the validation pipeline is:

| Step | Check | Covers lock? | Covers type? |
|---|---|---|---|
| `check_data()` (deserialization) | bit-pattern validity only | ✓ | ✓ |
| `ScriptHashTypeVerifier` (non-contextual) | enabled-set membership | ✓ | **✗ missing** |
| `select_version()` (script execution) | enabled-set membership | ✓ | ✓ |

The gap at the non-contextual layer means a transaction with a disallowed type-script hash type passes `NonContextualTransactionVerifier::verify()`: [4](#0-3) 

and is forwarded into the expensive contextual pipeline (cell-dep resolution, CKB-VM execution) before being rejected.

The existing unit tests confirm only the lock-script path is exercised: [5](#0-4) 

There is no corresponding test for a disallowed type-script hash type.

### Impact Explanation

An unprivileged RPC caller (`send_transaction`) can submit transactions whose outputs carry a type script with `Data3` (or any future disallowed hash type). These transactions:

1. Pass the cheap non-contextual gate (`ScriptHashTypeVerifier`) that is supposed to stop them.
2. Enter the expensive contextual verification path: cell-dep lookup in RocksDB, CKB-VM instantiation, and script execution.
3. Are ultimately rejected by `select_version()` with `InvalidScriptHashType`.

The attacker pays no fee (the transaction is never committed) but forces the node to perform full script-execution work per submission. This is a targeted resource-exhaustion (DoS) vector against the tx-pool verification worker.

### Likelihood Explanation

The attack requires only the ability to submit RPC transactions — available to any node operator or any peer that can reach the RPC endpoint. Crafting an output with `hash_type = 6` (`Data3`) is trivial. No key material, no mining power, and no privileged access is needed.

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's hash type when it is present:

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

where `check_hash_type` encapsulates the existing `TryInto` + `ENABLED_SCRIPT_HASH_TYPE` logic.

### Proof of Concept

1. Build a `CellOutput` whose `type_` field is a `Script` with `hash_type = 6` (`Data3`) and any `code_hash`.
2. Wrap it in a transaction and call `send_transaction` via JSON-RPC.
3. Observe: the node does **not** return `ScriptHashTypeNotPermitted` at the non-contextual stage; instead it proceeds to cell-dep resolution and script execution before returning `InvalidScriptHashType` from the VM layer.
4. Repeat in a tight loop — each iteration forces a full contextual verification cycle with no fee cost to the attacker.

### Citations

**File:** verification/src/transaction_verifier.rs (L94-102)
```rust
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
