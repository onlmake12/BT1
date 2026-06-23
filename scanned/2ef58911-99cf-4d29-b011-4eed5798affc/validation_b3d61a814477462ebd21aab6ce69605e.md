### Title
`ScriptHashTypeVerifier` Omits Type Script `hash_type` Validation, Allowing Unsupported Hash Types to Bypass Non-Contextual Checks — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` enforces that output cells' **lock** scripts use only enabled `hash_type` values, but it never inspects the **type** script's `hash_type`. This is a direct analog to the ERC777/ERC20 interface-assumption mismatch: just as the Swapnet `withdraw` assumed every ERC777 token implements ERC20's `transfer`, the CKB verifier assumes that checking the lock script is sufficient to enforce the `ENABLED_SCRIPT_HASH_TYPE` invariant across all scripts in an output cell.

---

### Finding Description

`ENABLED_SCRIPT_HASH_TYPE` permits only four values: `{0=Data, 1=Type, 2=Data1, 4=Data2}`. [1](#0-0) 

The `ScriptHashType` enum, however, is defined for all even values 0–254 and the value 1 (Data3=6, Data4=8, …, Data127=254). [2](#0-1) 

`ScriptHashTypeVerifier::verify()` iterates over every output and validates only `output.lock().hash_type()`. It never calls `output.type_().hash_type()`: [3](#0-2) 

The struct's own doc comment confirms the intent is broader ("Verify that the ScriptHashType of transaction outputs is within the range permitted by the current consensus rules"), yet the implementation silently skips type scripts. [4](#0-3) 

This verifier is the sole `hash_type` gate inside `NonContextualTransactionVerifier`, which is the first-pass check applied to every incoming transaction before script execution: [5](#0-4) 

When script execution later encounters the unsupported hash type, `select_version` returns `ScriptError::InvalidScriptHashType` for any `hash_type` not in `{Data, Data1, Data2, Type}`: [6](#0-5) 

The non-contextual gate therefore never rejects a transaction whose output carries a type script with `hash_type=6` (Data3), `hash_type=8` (Data4), etc.

---

### Impact Explanation

1. **Tx-pool pollution / resource exhaustion**: An unprivileged transaction sender can craft transactions whose outputs carry type scripts with unsupported `hash_type` values (e.g., `0x06` = Data3). These transactions pass `NonContextualTransactionVerifier` and are forwarded through the relay protocol and into the tx-pool admission pipeline. Each such transaction consumes relay bandwidth, tx-pool memory, and partial verification CPU before being rejected at the script-execution stage.

2. **Relay amplification**: Because the non-contextual check is the criterion used by the sync/relay layer to decide whether to propagate a transaction to peers, every peer that receives such a transaction will also relay it onward, amplifying the resource cost across the network.

3. **Inconsistent early-rejection semantics**: Any component that relies on `NonContextualTransactionVerifier` as a complete "is this transaction structurally valid?" gate (e.g., light-client protocol, RPC pre-validation) will give a false-positive for transactions that are ultimately invalid.

---

### Likelihood Explanation

The attack requires only the ability to submit a transaction via RPC or P2P relay — no privileged access, no key material, no majority hashpower. Constructing a transaction with a type script whose `hash_type` byte is `0x06` is trivial. The attacker pays no fee because the transaction never commits. This makes sustained tx-pool flooding cheap.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the `hash_type` of each output's optional type script:

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

This mirrors the existing pattern used in `CellbaseVerifier` for lock scripts and closes the gap between the stated intent of the verifier and its implementation. [7](#0-6) 

---

### Proof of Concept

1. Construct a transaction with one output whose **lock** script uses `hash_type=0` (Data, enabled) and whose **type** script uses `hash_type=6` (Data3, not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Submit via `send_transaction` RPC.
3. Observe that `NonContextualTransactionVerifier` (specifically `ScriptHashTypeVerifier`) returns `Ok(())` — no `ScriptHashTypeNotPermitted` or `InvalidScriptHashType` error is raised at this stage.
4. The transaction proceeds to script execution, where `select_version` returns `ScriptError::InvalidScriptHashType("The ScriptHashType/Data3 has not been activated…")`.
5. Repeat at high rate; each iteration passes the non-contextual gate and consumes relay/pool resources before being discarded.

The existing test suite confirms the lock-only scope: `test_not_enabled_hash_type_output_lock` passes a `Data3` lock and expects rejection, but no analogous test exists for a `Data3` **type** script — it would pass `ScriptHashTypeVerifier` without error. [8](#0-7)

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

**File:** verification/src/block_verifier.rs (L135-144)
```rust
        for output in cellbase_transaction.outputs() {
            if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
                let val: u8 = hash_type.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err((CellbaseError::InvalidOutputLock).into());
                }
            } else {
                return Err((CellbaseError::InvalidOutputLock).into());
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
