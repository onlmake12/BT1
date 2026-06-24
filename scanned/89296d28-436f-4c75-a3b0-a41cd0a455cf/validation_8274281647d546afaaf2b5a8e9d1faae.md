Audit Report

## Title
`ScriptHashTypeVerifier` Omits Output Type Script Hash-Type Check, Enabling Permanent Tx-Pool Pollution — (`verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` enforces `ENABLED_SCRIPT_HASH_TYPE` only on output **lock** scripts, never on output **type** scripts. An attacker can submit a transaction whose output type script carries an unactivated `ScriptHashType` variant (e.g., `Data3 = 6`). The transaction passes all non-contextual checks and is admitted to the tx-pool, but `select_version()` in `script/src/types.rs` returns `Err(InvalidScriptHashType)` for any such variant at execution time, making the transaction permanently unexecutable and permanently occupying a tx-pool slot.

## Finding Description

**`ENABLED_SCRIPT_HASH_TYPE` allowlist** (`util/constant/src/consensus.rs`, L7–12) permits only `{0, 1, 2, 4}`: [1](#0-0) 

**`ScriptHashType` enum** (`util/gen-types/src/core.rs`, L9–32) expands via `seq_macro` to include `Data3`–`Data127` (values 6–254). `verify_value()` (L39–41) accepts all even values and 1, so `Data3 = 6` passes structural validation: [2](#0-1) 

**`ScriptHashTypeVerifier::verify()`** (`verification/src/transaction_verifier.rs`, L796–814) iterates outputs and checks only `output.lock().hash_type()`. There is no inspection of `output.type_()`: [3](#0-2) 

This verifier is the sole `ENABLED_SCRIPT_HASH_TYPE` enforcement point in `NonContextualTransactionVerifier`, which is the gate used by the tx-pool: [4](#0-3) 

**At execution time**, `select_version()` (`script/src/types.rs`, L900–936) handles only `Data`, `Data1`, `Data2`, and `Type`. Any other variant falls to the catch-all arm: [5](#0-4) 

**Exploit path:**
1. Attacker constructs a transaction with an output whose type script has `hash_type = 6` (`Data3`).
2. `check_data()` passes (6 is even → `verify_value` returns `true`).
3. `ScriptHashTypeVerifier` passes (only lock script is checked).
4. Transaction is admitted to the tx-pool.
5. Every attempt to include it in a block calls `select_version()` on the type script → `Err(InvalidScriptHashType)` → block rejected.
6. Transaction remains in the tx-pool indefinitely, occupying a slot.

The existing tests confirm only lock-script coverage: [6](#0-5) 

Note: `CellbaseVerifier` in `block_verifier.rs` is not affected because cellbase outputs are required to have empty type scripts (L126–133): [7](#0-6) 

## Impact Explanation
**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker with any CKB UTXOs can continuously submit transactions with invalid type script hash types. Each transaction permanently occupies a tx-pool slot (or until TTL eviction, after which the attacker resubmits). With enough such transactions, the tx-pool fills with permanently unexecutable entries, displacing legitimate transactions and degrading network throughput. Miners that include such transactions assemble blocks rejected by all peers, forfeiting block rewards.

## Likelihood Explanation
Any unprivileged user can trigger this. The only requirement is owning CKB UTXOs to fund transaction inputs and fees. The `Data3` value (6) is a structurally valid even byte accepted by all parsing layers. No special access, key material, or majority hashpower is required. The attack is repeatable at the cost of standard transaction fees.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to also validate the hash type of each output's type script against `ENABLED_SCRIPT_HASH_TYPE`:

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

This mirrors the pattern already used in `CellbaseVerifier` for lock scripts. [3](#0-2) 

## Proof of Concept

```rust
#[test]
pub fn test_not_enabled_hash_type_output_type_script() {
    let type_script = Script::default()
        .as_builder()
        .hash_type(ScriptHashType::Data3) // value = 6, not in ENABLED_SCRIPT_HASH_TYPE
        .build();
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .lock(Script::default()) // valid lock (Data = 0)
                .type_(Some(type_script))
                .build(),
        )
        .build();
    let verifier = ScriptHashTypeVerifier::new(&transaction);
    // Currently passes — should return ScriptHashTypeNotPermitted
    assert!(verifier.verify().is_ok()); // BUG: should be Err
}
```

Submit via RPC `send_transaction`. The node runs `NonContextualTransactionVerifier` → `ScriptHashTypeVerifier` passes (lock is valid) → transaction enters tx-pool. Any block including it will fail contextual script execution with `InvalidScriptHashType` for the type script.

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

**File:** util/gen-types/src/core.rs (L39-41)
```rust
    pub fn verify_value(v: u8) -> bool {
        v.is_multiple_of(2) || v == 1
    }
```

**File:** verification/src/transaction_verifier.rs (L80-102)
```rust
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

**File:** script/src/types.rs (L930-935)
```rust
            hash_type => {
                return Err(ScriptError::InvalidScriptHashType(format!(
                    "The ScriptHashType/{:?} has not been activated, and is not permitted for use.",
                    hash_type
                )));
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

**File:** verification/src/block_verifier.rs (L126-133)
```rust
        // cellbase output type_ must be empty
        if cellbase_transaction
            .outputs()
            .into_iter()
            .any(|output| output.type_().is_some())
        {
            return Err((CellbaseError::InvalidTypeScript).into());
        }
```
