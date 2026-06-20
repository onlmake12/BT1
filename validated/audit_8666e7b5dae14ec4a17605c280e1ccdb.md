### Title
Incomplete `ScriptHashTypeVerifier` Omits Output Type Script Hash Type Validation — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` enforces that output script hash types fall within `ENABLED_SCRIPT_HASH_TYPE`, but only checks the **lock script** of each output. The **type script** hash type of outputs is never validated against the allowed set. A transaction sender can submit a transaction whose output carries a type script with a non-enabled `hash_type` (e.g., `Data3 = 6`), which passes `NonContextualTransactionVerifier` and is admitted to the tx-pool before being rejected during script execution.

---

### Finding Description

`ENABLED_SCRIPT_HASH_TYPE` is defined as the set `{0, 1, 2, 4}` (Data, Type, Data1, Data2): [1](#0-0) 

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and checks only `output.lock().hash_type()`: [2](#0-1) 

The type script of each output — `output.type_().to_opt()` — is never inspected. The `ScriptHashType` enum supports values up to `Data127 = 254` via the `seq!` macro: [3](#0-2) 

A transaction output with a type script using `Data3 = 6` (or any other even value ≥ 6) passes `ScriptHashTypeVerifier` because only the lock script is checked. The `NonContextualTransactionVerifier` — which is the first gate for tx-pool admission — includes `ScriptHashTypeVerifier` as its final step: [4](#0-3) 

The comment on `NonContextualTransactionVerifier` explicitly states "Check whether output lock hash type within enabled range," confirming the type script is intentionally omitted from this check. However, the type script hash type is also not validated anywhere else in the non-contextual path. Downstream, `select_version` and `extract_script_and_dep_index` in `script/src/types.rs` return `InvalidScriptHashType` for non-enabled values only at script execution time: [5](#0-4) 

This means the rejection of such a transaction is deferred to contextual script execution, not caught at the non-contextual admission gate.

---

### Impact Explanation

An unprivileged tx-pool submitter or RPC caller can craft a transaction with an output whose type script uses a non-enabled `hash_type` (e.g., `Data3`). This transaction:

1. Passes `NonContextualTransactionVerifier` (the tx-pool admission gate).
2. Is propagated to peers and admitted to the local tx-pool.
3. Consumes tx-pool memory and CPU resources.
4. Is only rejected when script execution runs during contextual verification.

Because the transaction fails script execution, the fee is never collected, making repeated submission cost-free for the attacker. This is a tx-pool resource exhaustion vector. Additionally, such transactions can be relayed to peers before rejection, amplifying the resource waste across the network.

---

### Likelihood Explanation

The entry path is fully unprivileged: any node accepting transactions via RPC (`send_transaction`) or the P2P relay protocol is reachable. Crafting a transaction with a non-enabled type script hash type requires no special knowledge or privilege — it is a trivial field manipulation. The `ScriptHashType` enum exposes `Data3` through `Data127` as valid Rust values, making it straightforward to construct such a transaction programmatically.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the `hash_type` of the type script of each output, when a type script is present:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // Check lock script hash type (existing)
        let lock_hash_type = output.lock().hash_type();
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(lock_hash_type) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err(TransactionError::InvalidScriptHashType { hash_type: lock_hash_type }.into());
        }

        // Check type script hash type (missing today)
        if let Some(type_script) = output.type_().to_opt() {
            let type_hash_type = type_script.hash_type();
            if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_hash_type) {
                let val: u8 = hash_type.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
                }
            } else {
                return Err(TransactionError::InvalidScriptHashType { hash_type: type_hash_type }.into());
            }
        }
    }
    Ok(())
}
```

---

### Proof of Concept

A transaction sender submits via RPC:

```json
{
  "method": "send_transaction",
  "params": [{
    "version": "0x0",
    "inputs": [{ "previous_output": { "tx_hash": "<valid_utxo>", "index": "0x0" }, "since": "0x0" }],
    "outputs": [{
      "capacity": "0x...",
      "lock": { "code_hash": "0x...", "hash_type": "type", "args": "0x..." },
      "type": { "code_hash": "0x...", "hash_type": "data3", "args": "0x" }
    }],
    "outputs_data": ["0x"],
    "cell_deps": [], "header_deps": [], "witnesses": ["0x"]
  }]
}
```

`hash_type: "data3"` corresponds to `ScriptHashType::Data3 = 6`, which is not in `ENABLED_SCRIPT_HASH_TYPE`. The transaction passes `NonContextualTransactionVerifier` (lock script hash type `"type" = 1` is valid; type script hash type is never checked) and enters the tx-pool. It is only rejected when script execution attempts to resolve the type script and calls `select_version`, which returns `InvalidScriptHashType` for `Data3`. [6](#0-5) [1](#0-0) [7](#0-6)

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
