Audit Report

## Title
`ScriptHashTypeVerifier` Omits Type Script Hash-Type Check, Enabling Uncommittable Transactions in Tx-Pool — (File: verification/src/transaction_verifier.rs)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and enforces the `ENABLED_SCRIPT_HASH_TYPE` allowlist only on each output's lock script, never on the type script. A transaction whose output carries `hash_type = 0x06` (`Data3`) on its type script passes all non-contextual checks and is admitted to the tx-pool, yet can never be committed to a valid block because `select_version()` rejects the unknown hash type at script execution time. This creates a persistent tx-pool pollution vector reachable by any RPC caller.

## Finding Description

`NonContextualTransactionVerifier` invokes `ScriptHashTypeVerifier` as its final gate: [1](#0-0) 

`ScriptHashTypeVerifier::verify()` loops over outputs and checks only `output.lock().hash_type()`. The `output.type_()` field is never read: [2](#0-1) 

`ENABLED_SCRIPT_HASH_TYPE` is `{0, 1, 2, 4}` — `Data3` (value `6`) is absent: [3](#0-2) 

`ScriptHashType::verify_value()` accepts any even value or `1`, so `0x06` passes structural validation: [4](#0-3) 

`check_data()` for `ScriptOptReader` (used in the relay/sync layer) calls only `verify_value()`, not the consensus allowlist — so `0x06` on a type script passes that gate too: [5](#0-4) 

`ScriptHashType::try_from(6)` succeeds and returns `Data3`, so the `else` branch in `ScriptHashTypeVerifier::verify()` is never triggered. The transaction enters the tx-pool. At block-validation time, `select_version()` hits the catch-all arm and returns `InvalidScriptHashType` for `Data3`: [6](#0-5) 

The block is rejected. `BlockTxsVerifier` goes directly to `ContextualTransactionVerifier` and never re-runs `NonContextualTransactionVerifier`, so the gap is never compensated. The transaction occupies a tx-pool slot indefinitely until eviction.

The existing test suite covers only the lock script path: [7](#0-6) 

There is no analogous test for the type script path, and no code that checks it.

## Impact Explanation

This matches **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. An attacker with a modest UTXO set can continuously submit transactions whose type scripts carry `hash_type = 0x06`. These transactions occupy tx-pool slots indefinitely (until eviction timeout). Because the tx-pool treats the referenced inputs as pending, the attacker can recycle UTXOs after eviction and repeat, sustaining pool pressure. Legitimate transactions are displaced, degrading throughput for all users.

## Likelihood Explanation

The attack requires only the ability to call `send_transaction` via the public JSON-RPC interface — no keys beyond owning some UTXOs, no hashpower, no Sybil capability. Constructing the malformed transaction is trivial: set any output's type script `hash_type` byte to `0x06`. The attacker's cost is proportional to their UTXO count and the tx-pool eviction interval, both of which are low barriers.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to check the type script's `hash_type` for each output, mirroring the existing lock-script check:

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

Also update the struct-level doc comment and the `NonContextualTransactionVerifier` doc comment to reflect that both lock and type scripts are checked. [8](#0-7) [9](#0-8) 

## Proof of Concept

1. Obtain any unspent cell (UTXO) on a CKB testnet node.
2. Construct a transaction spending that cell, with one output whose `lock` uses `hash_type = 0x01` (Type, valid) and whose `type_` uses `hash_type = 0x06` (`Data3`, not in `ENABLED_SCRIPT_HASH_TYPE`).
3. Submit via `send_transaction` RPC.
4. **Expected (buggy) result**: the call succeeds; the transaction appears in `get_raw_tx_pool`.
5. Attempt to mine a block containing this transaction (or wait for a miner to pick it up).
6. **Expected result**: block validation fails; the transaction is never committed.
7. The tx-pool slot remains occupied. Repeat with additional UTXOs to exhaust pool capacity and evict legitimate transactions.

A unit test can be added to `verification/src/tests/transaction_verifier.rs` mirroring `test_not_enabled_hash_type_output_lock` but setting the type script's `hash_type` to `ScriptHashType::Data3` and asserting `ScriptHashTypeNotPermitted` is returned — currently this assertion would **fail**, confirming the bug. [7](#0-6)

### Citations

**File:** verification/src/transaction_verifier.rs (L70-70)
```rust
/// - Check whether output lock hash type within enabled range
```

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

**File:** verification/src/transaction_verifier.rs (L785-787)
```rust
// Verify that the ScriptHashType of transaction outputs
// is within the range permitted by the current consensus rules.
pub struct ScriptHashTypeVerifier<'a> {
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

**File:** util/gen-types/src/core.rs (L39-41)
```rust
    pub fn verify_value(v: u8) -> bool {
        v.is_multiple_of(2) || v == 1
    }
```

**File:** util/gen-types/src/extension/check_data.rs (L16-27)
```rust
impl<'r> packed::ScriptOptReader<'r> {
    fn check_data(&self) -> bool {
        self.to_opt()
            .map(|i| core::ScriptHashType::verify_value(i.hash_type().into()))
            .unwrap_or(true)
    }
}

impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
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
