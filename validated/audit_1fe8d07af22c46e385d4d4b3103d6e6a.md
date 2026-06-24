Audit Report

## Title
`ScriptHashTypeVerifier` Omits Type Script `hash_type` Validation, Allowing Future Hash Types to Bypass Non-Contextual Checks - (File: `verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates only the lock script's `hash_type` against `ENABLED_SCRIPT_HASH_TYPE`, completely skipping the type script. A transaction output carrying a type script with a future/not-yet-enabled `hash_type` (e.g., `Data3 = 6`) silently passes the non-contextual gate, enters the tx-pool verify queue, and is only rejected during contextual verification — after the peer should have been banned. This creates an asymmetric peer-banning path and allows verify-queue pollution at near-zero cost.

## Finding Description

`ScriptHashTypeVerifier::verify()` only inspects `output.lock().hash_type()`:

```rust
// verification/src/transaction_verifier.rs L796-814
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(...ScriptHashTypeNotPermitted { hash_type: val }...);
            }
        } else { ... }
    }
    Ok(())
}
```

`output.type_()` is never inspected. [1](#0-0) 

`ENABLED_SCRIPT_HASH_TYPE` is `{0, 1, 2, 4}` (Data, Type, Data1, Data2). [2](#0-1) 

`ScriptHashType` is generated via `seq!` macro and includes `Data3 = 6`, `Data4 = 8`, … `Data127 = 254`. [3](#0-2) 

The P2P-layer `check_data()` for `CellOutputReader` calls `self.lock().check_data() && self.type_().check_data()`, where `check_data()` uses `verify_value()` which accepts any even number or 1 — so `Data3 = 6` passes. [4](#0-3) [5](#0-4) 

**Exploit path:**

1. Attacker crafts a transaction with a valid lock script (`hash_type = 0`) and a type script with `hash_type = 6` (Data3). Inputs can reference a real UTXO (reusable across attempts since rejected txs don't consume UTXOs).
2. `check_data()` passes: `verify_value(6)` = true (6 is even).
3. `ScriptHashTypeVerifier::verify()` passes: only lock script is checked.
4. Transaction enters the verify queue via `enqueue_verify_queue`. [6](#0-5) 
5. During contextual verification, `TxData::new()` builds type groups from outputs (lines 733–739), creating a script group for the invalid type script. [7](#0-6) 
6. `verify_script_group()` → `run()` → `create_scheduler()` → `select_version()` is called, which hits the catch-all arm and returns `ScriptError::InvalidScriptHashType` — **before** the VM actually executes. [8](#0-7) 
7. The error is classified as `ErrorKind::Script`, which `is_malformed_from_verification()` treats as malformed, triggering a peer ban — but only after contextual verification, not at the non-contextual gate. [9](#0-8) 

**Correction to the claim's impact wording:** The VM is NOT actually run. `select_version()` fails during scheduler creation, before `scheduler.run()` is called. The overhead is contextual verification setup, not full script execution. The claim's phrase "spin up the CKB-VM scheduler" is accurate; "script execution" is an overstatement.

**Existing checks are insufficient:** `NonContextualTransactionVerifier` explicitly documents only checking the lock hash type, confirming the gap is unintentional. [10](#0-9) 

## Impact Explanation

**Applicable impact: High — bad design which could cause CKB network congestion with few costs.**

- **Verify queue pollution:** The attacker can submit transactions with a single real UTXO (reused across attempts from different peer connections/IPs, since rejected transactions do not consume UTXOs). Each transaction passes non-contextual verification and occupies a verify queue slot, degrading throughput for legitimate transactions.
- **Asymmetric peer banning:** For lock scripts with invalid `hash_type`, the peer is banned immediately at the non-contextual stage. For type scripts with invalid `hash_type`, the peer is only banned after contextual verification completes. An attacker cycling through peer connections (Tor/VPN) can sustain the attack with a single UTXO at near-zero on-chain cost.
- **Incomplete non-contextual gate:** `ScriptHashTypeVerifier` is the canonical cheap filter for hash_type validity. Its failure to cover type scripts means the distinction between "valid" and "invalid" hash_types is broken for type scripts, forcing the more expensive contextual path for what should be a trivially rejectable transaction.

## Likelihood Explanation

Any unprivileged P2P relay submitter or RPC caller can craft a transaction with a type script `hash_type = 6`. The transaction is structurally valid (passes `check_data()`), requires no special privileges, and can be submitted repeatedly from different peer connections reusing the same UTXO. No fees are paid since the transaction is never included in a block. The attack is low-cost and repeatable.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the `hash_type` of each output's type script:

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

fn check_hash_type(hash_type: packed::Byte) -> Result<(), Error> {
    if let Ok(ht) = TryInto::<ScriptHashType>::try_into(hash_type) {
        let val: u8 = ht.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
        }
    } else {
        return Err(TransactionError::InvalidScriptHashType { hash_type }.into());
    }
    Ok(())
}
```

Also update the `NonContextualTransactionVerifier` docstring to say *"lock and type"* rather than just *"lock"*. [11](#0-10) 

## Proof of Concept

```rust
#[test]
pub fn test_not_enabled_hash_type_output_type_script_bypasses_verifier() {
    use ckb_types::{
        core::{ScriptHashType, TransactionBuilder},
        packed::{CellOutput, Script, ScriptOpt},
        prelude::*,
    };
    use crate::transaction_verifier::ScriptHashTypeVerifier;

    let valid_lock = Script::default(); // hash_type = Data = 0
    let invalid_type = Script::default()
        .as_builder()
        .hash_type(ScriptHashType::Data3) // value = 6, not in ENABLED_SCRIPT_HASH_TYPE
        .build();

    let output = CellOutput::new_builder()
        .lock(valid_lock)
        .type_(ScriptOpt::new_builder().set(Some(invalid_type)).build())
        .build();

    let transaction = TransactionBuilder::default().output(output).build();
    let verifier = ScriptHashTypeVerifier::new(&transaction);

    // BUG: returns Ok(()) — type script hash_type is never checked
    assert!(verifier.verify().is_ok(), "type script hash_type bypass confirmed");
}
```

**Expected:** `Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: 6 })`
**Actual:** `Ok(())` — the transaction proceeds to the verify queue where contextual verification rejects it at `select_version()` (before VM execution), but only after a verify queue slot is consumed and the peer ban is delayed.

### Citations

**File:** verification/src/transaction_verifier.rs (L61-70)
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

**File:** util/gen-types/src/extension/check_data.rs (L10-13)
```rust
impl<'r> packed::ScriptReader<'r> {
    fn check_data(&self) -> bool {
        core::ScriptHashType::verify_value(self.hash_type().into())
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

**File:** tx-pool/src/process.rs (L341-352)
```rust
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
        self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
```

**File:** script/src/types.rs (L733-739)
```rust
        for (i, output) in rtx.transaction.outputs().into_iter().enumerate() {
            if let Some(t) = &output.type_().to_opt() {
                let type_group_entry = type_groups
                    .entry(t.calc_script_hash())
                    .or_insert_with(|| ScriptGroup::from_type_script(t));
                type_group_entry.output_indices.push(i);
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

**File:** util/types/src/core/tx_pool.rs (L69-85)
```rust
fn is_malformed_from_verification(error: &Error) -> bool {
    match error.kind() {
        ErrorKind::Transaction => error
            .downcast_ref::<TransactionError>()
            .expect("error kind checked")
            .is_malformed_tx(),
        ErrorKind::Script => !format!("{}", error).contains(ARGV_TOO_LONG_TEXT),
        ErrorKind::Internal => {
            error
                .downcast_ref::<InternalError>()
                .expect("error kind checked")
                .kind()
                == InternalErrorKind::CapacityOverflow
        }
        _ => false,
    }
}
```
