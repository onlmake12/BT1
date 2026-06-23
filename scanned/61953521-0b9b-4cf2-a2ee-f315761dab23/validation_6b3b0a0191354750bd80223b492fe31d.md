### Title
`ScriptHashTypeVerifier` Does Not Check Type Scripts of Transaction Outputs Against `ENABLED_SCRIPT_HASH_TYPE` — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` enforces the `ENABLED_SCRIPT_HASH_TYPE` allowlist only on the **lock script** of each transaction output. The **type script** of each output is never checked. A transaction sender can craft an output cell whose type script carries a non-permitted `ScriptHashType` (e.g., `Data3` = `0x06`) and the non-contextual gate will pass it silently, allowing the transaction to enter the tx pool before being rejected during script execution.

---

### Finding Description

`ENABLED_SCRIPT_HASH_TYPE` is a compile-time set containing only the four currently activated values:

```
{ 0 = Data, 1 = Type, 2 = Data1, 4 = Data2 }
```

The `ScriptHashType` enum, however, is generated with variants `Data3` through `Data127` (values `6`, `8`, …, `254`).

`ScriptHashTypeVerifier::verify()` iterates over every output and inspects only `output.lock().hash_type()`:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else { … }
    }
    Ok(())
}
```

There is no corresponding branch for `output.type_().to_opt()`. The comment on `NonContextualTransactionVerifier` acknowledges only the lock-script check ("Check whether output lock hash type within enabled range"), confirming the type-script path was never added.

The low-level format gate (`ScriptHashType::verify_value`) only checks that the byte is even-or-one, so `Data3 = 6` passes it. The transaction therefore clears all non-contextual checks and is admitted to the tx pool. Only when `TransactionScriptsVerifier` later calls `select_version()` does the type script's hash type hit the catch-all arm and return `InvalidScriptHashType`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

---

### Impact Explanation

Any unprivileged transaction sender can submit a transaction whose output cell carries a type script with `hash_type = Data3` (or any even value ≥ 6). The transaction:

1. Passes `NonContextualTransactionVerifier` — the `ScriptHashTypeVerifier` only inspects lock scripts.
2. Is admitted to the tx pool, consuming pool slots and triggering contextual verification work.
3. Fails in `select_version()` with `InvalidScriptHashType` and is evicted as malformed.

Because the rejection happens after pool admission, an attacker can repeatedly inject structurally valid but semantically malformed transactions at zero fee cost (malformed transactions are never committed, so no fee is deducted). This degrades tx-pool throughput and wastes CPU cycles on every full node that relays or re-verifies the transaction. [5](#0-4) [6](#0-5) 

---

### Likelihood Explanation

The entry path requires no privilege: any RPC caller or P2P peer can submit a raw transaction. Crafting the malformed type script requires only setting the `hash_type` byte of a `Script` struct to `0x06` (`Data3`). The `check_data` structural validator accepts it because `verify_value(6)` returns `true` (6 is even). No key material, miner cooperation, or Sybil capability is needed. [7](#0-6) [8](#0-7) 

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script of each output when one is present:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock-script check
        check_hash_type(output.lock().hash_type())?;

        // NEW: type-script check
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}
```

where `check_hash_type` encapsulates the existing `TryInto` + `ENABLED_SCRIPT_HASH_TYPE` logic. This mirrors the completeness already present in `spec/src/lib.rs`'s `check_block`, which handles `Data`, `Type`, `Data1`, and `Data2` for lock scripts in the genesis block. [9](#0-8) 

---

### Proof of Concept

1. Build a transaction with one output whose **lock** script uses `ScriptHashType::Data` (permitted) and whose **type** script uses `hash_type = 0x06` (`Data3`, not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Submit via `send_transaction` RPC.
3. Observe that `NonContextualTransactionVerifier::verify()` returns `Ok(())` — `ScriptHashTypeVerifier` never inspects the type script.
4. The transaction enters the pending pool.
5. Contextual verification calls `select_version()` on the type script; the `hash_type => { Err(InvalidScriptHashType …) }` arm fires and the transaction is evicted as malformed.

The existing test `test_not_enabled_hash_type_output_lock` (which uses `Data3` on a **lock** script and expects `ScriptHashTypeNotPermitted`) has no counterpart for a **type** script, confirming the gap is untested and unguarded. [10](#0-9) [1](#0-0)

### Citations

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

**File:** util/gen-types/src/core.rs (L39-41)
```rust
    pub fn verify_value(v: u8) -> bool {
        v.is_multiple_of(2) || v == 1
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

**File:** util/gen-types/src/extension/check_data.rs (L10-13)
```rust
impl<'r> packed::ScriptReader<'r> {
    fn check_data(&self) -> bool {
        core::ScriptHashType::verify_value(self.hash_type().into())
    }
```

**File:** spec/src/lib.rs (L700-745)
```rust
            match ScriptHashType::try_from(lock_script.hash_type()).expect("checked data") {
                ScriptHashType::Data => {
                    if !data_hashes.contains_key(&lock_script.code_hash()) {
                        return Err(format!(
                            "Invalid lock script: code_hash={}, hash_type=data",
                            lock_script.code_hash(),
                        )
                        .into());
                    }
                }
                ScriptHashType::Type => {
                    if !type_hashes.contains_key(&lock_script.code_hash()) {
                        return Err(format!(
                            "Invalid lock script: code_hash={}, hash_type=type",
                            lock_script.code_hash(),
                        )
                        .into());
                    }
                }
                ScriptHashType::Data1 => {
                    if !data_hashes.contains_key(&lock_script.code_hash()) {
                        return Err(format!(
                            "Invalid lock script: code_hash={}, hash_type=data1",
                            lock_script.code_hash(),
                        )
                        .into());
                    }
                }
                ScriptHashType::Data2 => {
                    if !data_hashes.contains_key(&lock_script.code_hash()) {
                        return Err(format!(
                            "Invalid lock script: code_hash={}, hash_type=data2",
                            lock_script.code_hash(),
                        )
                        .into());
                    }
                }
                hash_type => {
                    return Err(format!(
                        "Invalid lock script: code_hash={}, hash_type={:?}",
                        lock_script.code_hash(),
                        hash_type
                    )
                    .into());
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
