Audit Report

## Title
Missing Type Script `hash_type` Validation in `ScriptHashTypeVerifier::verify()` — (`verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates every transaction output but only validates the `hash_type` of the **lock script**, never the optional **type script**. An unprivileged attacker can submit a transaction whose output type script carries a structurally valid but consensus-prohibited `hash_type` (e.g., `Data3 = 6`), bypass the designated early-rejection gate, pollute the tx pool, and force expensive contextual verification that would otherwise be avoided.

## Finding Description

`ScriptHashTypeVerifier` is the sole enforcer of `ENABLED_SCRIPT_HASH_TYPE` for transaction outputs and is invoked as part of `NonContextualTransactionVerifier`: [1](#0-0) 

`ENABLED_SCRIPT_HASH_TYPE` is defined as `{0, 1, 2, 4}` — only `Data`, `Type`, `Data1`, `Data2` are permitted: [2](#0-1) 

The `verify()` loop reads only `output.lock().hash_type()` and never touches `output.type_()`: [3](#0-2) 

A type script with `hash_type = 6` (`Data3`) is structurally valid — `verify_value` accepts any even byte or `1` — so it passes `check_data` on the relay/sync path. But `check_data` does not enforce `ENABLED_SCRIPT_HASH_TYPE`; only `ScriptHashTypeVerifier` does, and it skips type scripts entirely.

The gap is confirmed by the existing test suite: `test_not_enabled_hash_type_output_lock` covers the lock script case but no counterpart exists for type scripts: [4](#0-3) 

The non-activated hash type is only caught later in `select_version` during script execution: [5](#0-4) 

Script execution is orders of magnitude more expensive than the non-contextual check, and it occurs after tx-pool admission.

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker can cheaply and repeatedly craft transactions with type scripts carrying `hash_type = 6` and submit them via the public RPC. Each transaction:
1. Passes `NonContextualTransactionVerifier` (the cheap gate) and enters the tx pool.
2. Consumes tx-pool admission resources and occupies pool slots.
3. Is only rejected during contextual script execution — an expensive operation — wasting node CPU.

With no rate-limiting on the structural validity of type script hash types, an attacker can sustain a low-cost flood that degrades tx-pool throughput and node responsiveness across the network.

Additionally, if a miner's node includes such a transaction in a block template, the resulting block fails script verification on all validating nodes, wasting the miner's PoW work.

## Likelihood Explanation

Any unprivileged RPC caller can trigger this. The `hash_type` field is a single byte in the Molecule-encoded `Script` struct. Setting it to `6` requires no keys, no special privileges, and no majority hash power. The value passes Molecule deserialization and `check_data` (structural check) without error. The attack is trivially repeatable and automatable.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to validate the type script `hash_type` when a type script is present, mirroring the completeness already in `check_data`: [6](#0-5) 

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        Self::check_script_hash_type(output.lock().hash_type())?;
        if let Some(type_script) = output.type_().to_opt() {
            Self::check_script_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}
```

Add a corresponding test `test_not_enabled_hash_type_output_type` parallel to the existing lock-script test.

## Proof of Concept

1. Build a `CellOutput` with a type script whose `hash_type` byte is `6` (`ScriptHashType::Data3`).
2. Wrap it in a transaction and submit via `send_transaction` RPC.
3. Observe that `NonContextualTransactionVerifier` (including `ScriptHashTypeVerifier`) returns `Ok(())`.
4. Confirm the transaction is admitted to the tx pool.
5. Observe rejection only occurs later during contextual script execution at `select_version`, after significantly more node resources have been consumed.

The existing unit test infrastructure in `verification/src/tests/transaction_verifier.rs` can be extended directly — construct a `CellOutput` with a type script set to `ScriptHashType::Data3`, call `ScriptHashTypeVerifier::new(&transaction).verify()`, and assert it currently (incorrectly) returns `Ok(())`.

### Citations

**File:** verification/src/transaction_verifier.rs (L94-101)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        self.version.verify()?;
        self.size.verify()?;
        self.empty.verify()?;
        self.duplicate_deps.verify()?;
        self.outputs_data_verifier.verify()?;
        self.script_hash_type.verify()?;
        Ok(())
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

**File:** util/gen-types/src/extension/check_data.rs (L24-28)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
```
