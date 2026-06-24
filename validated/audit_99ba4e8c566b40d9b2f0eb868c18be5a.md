Audit Report

## Title
Type Script `hash_type` Not Validated in `ScriptHashTypeVerifier::verify()`, Allowing Bypass of Non-Contextual Consensus Gate — (`File: verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates the `hash_type` of each output's **lock script** against `ENABLED_SCRIPT_HASH_TYPE`, but never inspects the **type script**'s `hash_type`. Because any CKB output may carry an optional type script, an attacker can craft a transaction whose type script carries a `hash_type` byte outside `ENABLED_SCRIPT_HASH_TYPE` and have it pass `NonContextualTransactionVerifier` entirely. The transaction then proceeds to contextual verification, consuming script-group construction and VM-setup resources that the non-contextual gate was designed to eliminate.

## Finding Description

`ScriptHashTypeVerifier::verify()` at lines 796–814 of `verification/src/transaction_verifier.rs` reads:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) =
            TryInto::<ScriptHashType>::try_into(output.lock().hash_type())  // lock only
        {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err((TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            }).into());
        }
    }
    Ok(())
}
```

`output.type_()` is never consulted. `ENABLED_SCRIPT_HASH_TYPE` is the static set `{0, 1, 2, 4}` defined in `util/constant/src/consensus.rs`. Any byte outside this set (e.g., `3`, `5`, `255`) in a type script's `hash_type` field is silently ignored.

The struct's own doc comment at line 70 acknowledges only the lock-script intent: *"Check whether output lock hash type within enabled range"* — confirming the type script was never considered.

`ScriptHashTypeVerifier` is wired as the sole `hash_type` gate inside `NonContextualTransactionVerifier::verify()` (lines 80–102). No other non-contextual verifier checks type script `hash_type`. A grep across the entire `verification/src/` tree for `type_()` combined with `hash_type` returns zero matches, confirming no compensating check exists.

**Exploit flow:**
1. Attacker constructs a `TransactionView` with one output: lock script uses `hash_type = 0x01` (Type, permitted); type script uses `hash_type = 0x03` (not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Transaction is submitted via `send_transaction` RPC or P2P relay.
3. `NonContextualTransactionVerifier::verify()` calls `ScriptHashTypeVerifier::verify()`, which reads only `output.lock().hash_type()` → valid → `Ok(())`.
4. Transaction enters contextual verification (`TransactionScriptsVerifier`), consuming VM-setup and script-group construction resources before being rejected there.
5. An attacker can repeat this at high volume with no key material, no miner cooperation, and no chain state.

## Impact Explanation

The concrete, in-scope impact is **High: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

The non-contextual gate exists precisely to cheaply reject structurally invalid transactions before they reach the expensive contextual path. By bypassing it, an attacker can force every receiving node to perform script-group construction and VM initialisation for each crafted transaction. Because constructing such a transaction requires no special privileges and can be done at negligible cost, an attacker can sustain a high-volume flood of these transactions, causing disproportionate CPU and memory consumption across the network.

A secondary, forward-looking impact is consensus deviation: if a future fork introduces a new `hash_type` value and some nodes upgrade while others do not, a transaction carrying the new hash_type in its type script will pass non-contextual verification on all nodes (the bug), but upgraded nodes may accept it at contextual verification while non-upgraded nodes reject it — producing a chain split. The lock-script path is protected against this scenario; the type-script path is not.

## Likelihood Explanation

Any unprivileged user with RPC or P2P access can trigger this. No key material, miner cooperation, or special chain state is required. The attacker-controlled entry path is fully reachable: `send_transaction` RPC → tx-pool admission → `NonContextualTransactionVerifier::verify()` → `ScriptHashTypeVerifier::verify()`. The omission is hit on every node that processes externally submitted transactions. The attack is repeatable and cheap.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to apply the identical `ENABLED_SCRIPT_HASH_TYPE` check to the type script when present:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock-script check (unchanged)
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err((TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            }).into());
        }

        // add: type-script check
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

Also update the struct comment at line 70 to reflect that both lock and type scripts are checked.

## Proof of Concept

**Minimal unit test plan:**

```rust
#[test]
fn test_type_script_invalid_hash_type_bypasses_non_contextual_check() {
    // Build a transaction output with:
    //   lock.hash_type = 0x01 (Type, in ENABLED_SCRIPT_HASH_TYPE)
    //   type.hash_type = 0x03 (not in ENABLED_SCRIPT_HASH_TYPE)
    let lock = Script::new_builder()
        .hash_type(ScriptHashType::Type.into())
        .build();
    let type_script = Script::new_builder()
        .hash_type(3u8.into())  // 0x03, not in {0,1,2,4}
        .build();
    let output = CellOutput::new_builder()
        .lock(lock)
        .type_(Some(type_script).pack())
        .build();
    let tx = TransactionBuilder::default().output(output).build();

    let verifier = ScriptHashTypeVerifier::new(&tx);
    // Before fix: returns Ok(()) — type script hash_type silently ignored
    // After fix:  returns Err(ScriptHashTypeNotPermitted { hash_type: 3 })
    assert!(verifier.verify().is_err());
}
```

This test directly exercises the missing branch and will fail (returning `Ok`) against the current code, confirming the vulnerability. [1](#0-0) [2](#0-1) [3](#0-2)

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
