The code confirms all claims in the report. `ScriptHashType::Data3 = 3 << 1 = 6` (from the `seq!` macro), `verify_value(6)` returns `true` (6 is even), `ScriptHashTypeVerifier::verify()` only checks `output.lock().hash_type()`, and `ENABLED_SCRIPT_HASH_TYPE = {0, 1, 2, 4}` does not include 6. The bug is real and all cited line numbers are accurate.

Audit Report

## Title
`ScriptHashTypeVerifier::verify` Omits Type Script Hash-Type Check on Transaction Outputs — (File: `verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates only `output.lock().hash_type()` against `ENABLED_SCRIPT_HASH_TYPE`. The optional `output.type_()` field is never inspected. A transaction whose output carries a type script with an unenabled but structurally valid hash type (e.g., `Data3 = 6`) passes all non-contextual checks, enters the tx-pool verify queue, and is only rejected during contextual script execution — after the node has already spent resources resolving inputs and setting up the script verifier.

## Finding Description

`ScriptHashTypeVerifier::verify()` at `verification/src/transaction_verifier.rs` lines 796–814 loops over outputs and checks only the lock script: [1](#0-0) 

The type script is never examined. The lower-level `check_data` gate for `ScriptOptReader` only calls `verify_value`, which accepts any even byte or `1`: [2](#0-1) 

`ScriptHashType::Data3` is defined as `3 << 1 = 6` via the `seq!` macro: [3](#0-2) 

So `hash_type = 6` is even → passes `verify_value` → passes `check_data` → passes `ScriptHashTypeVerifier::verify()`. The `ENABLED_SCRIPT_HASH_TYPE` set is `{0, 1, 2, 4}` and does not include `6`: [4](#0-3) 

The transaction then enters the tx-pool via `non_contextual_verify`: [5](#0-4) 

It is enqueued for contextual verification where the invalid hash type is finally caught during script execution setup. By that point the node has already resolved inputs, allocated queue slots, and begun script verification. `ScriptHashTypeVerifier` is the sole hash-type gate in `NonContextualTransactionVerifier`: [6](#0-5) 

The existing test coverage confirms only the lock-script path is tested: [7](#0-6) 

## Impact Explanation

An attacker with any valid UTXOs can craft transactions with a valid lock script and a type script carrying `hash_type = 6` (or any other even, non-enabled value). Because the UTXO is not consumed on rejection, the attacker can create many distinct transactions (varying outputs/data) reusing the same UTXO set. Each transaction passes non-contextual verification, occupies a verify-queue slot, triggers input resolution, and forces partial contextual verification before being evicted. This enables sustained tx-pool queue saturation and CPU waste at negligible on-chain cost, matching the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**.

## Likelihood Explanation

The attack is reachable via the standard `send_transaction` RPC or P2P relay — both accessible to any unprivileged user. Constructing the malformed transaction requires only setting `hash_type = 6` on an output's type script using any CKB transaction builder. No special keys, privileges, or network position are required. The attacker's UTXOs are not consumed on rejection, enabling repeated submission of distinct (but structurally similar) transactions.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to mirror the lock-script check for the optional type script on each output:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // Existing lock script check
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
            }).into());
        }

        // Add: type script check
        if let Some(type_script) = output.type_().to_opt() {
            if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_script.hash_type()) {
                let val: u8 = hash_type.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err(
                        TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into(),
                    );
                }
            } else {
                return Err((TransactionError::InvalidScriptHashType {
                    hash_type: type_script.hash_type(),
                }).into());
            }
        }
    }
    Ok(())
}
```

Also update the doc comment on `NonContextualTransactionVerifier` (line 70) to reflect that both lock and type script hash types are checked. [8](#0-7) 

## Proof of Concept

Add the following test to `verification/src/tests/transaction_verifier.rs`, analogous to the existing `test_not_enabled_hash_type_output_lock`:

```rust
#[test]
pub fn test_not_enabled_hash_type_output_type_script() {
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .lock(Script::default()) // valid lock: Data = 0
                .type_(Some(
                    Script::default()
                        .as_builder()
                        .hash_type(ScriptHashType::Data3) // 6, not in ENABLED_SCRIPT_HASH_TYPE
                        .build(),
                ).pack())
                .build(),
        )
        .output_data(Bytes::new().pack())
        .build();

    let verifier = ScriptHashTypeVerifier::new(&transaction);

    // Currently returns Ok(()); should return ScriptHashTypeNotPermitted { hash_type: 6 }
    assert_error_eq!(
        verifier.verify().unwrap_err(),
        TransactionError::ScriptHashTypeNotPermitted {
            hash_type: ScriptHashType::Data3.into(),
        },
    );
}
```

Running this test against the current code will show `verifier.verify()` returns `Ok(())`, confirming the missing check.

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

**File:** util/gen-types/src/core.rs (L28-31)
```rust
        #(
            #[doc = concat!("Type \"data", stringify!(N), "\" matches script code via cell data hash, and runs the script code in v", stringify!(N), " CKB VM.")]
            Data~N = N << 1,
        )*
```

**File:** util/gen-types/src/core.rs (L39-41)
```rust
    pub fn verify_value(v: u8) -> bool {
        v.is_multiple_of(2) || v == 1
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

**File:** tx-pool/src/util.rs (L56-62)
```rust
pub(crate) fn non_contextual_verify(
    consensus: &Consensus,
    tx: &TransactionView,
) -> Result<(), Reject> {
    NonContextualTransactionVerifier::new(tx, consensus)
        .verify()
        .map_err(Reject::Verification)?;
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
