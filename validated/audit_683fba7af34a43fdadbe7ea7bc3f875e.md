Audit Report

## Title
`ScriptHashTypeVerifier` Omits Type-Script Hash-Type Validation on Transaction Outputs — (`verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates only the lock script's `hash_type`, leaving the optional type script entirely unchecked. A transaction carrying an output with a syntactically valid but consensus-disallowed type-script `hash_type` (e.g., `3`) passes `NonContextualTransactionVerifier`, enters the tx-pool, and can be mined into a block. Once on-chain, the resulting live cell is permanently unspendable. If a miner node mines such a transaction, nodes receiving the block via the P2P relay path will reject it through `check_data`, producing a consensus split.

## Finding Description

`ScriptHashTypeVerifier::verify()` at lines 796–814 of `verification/src/transaction_verifier.rs` loops over outputs and calls `TryInto::<ScriptHashType>::try_into(output.lock().hash_type())`, then checks the result against `ENABLED_SCRIPT_HASH_TYPE`. The type script branch (`output.type_()`) is never touched. [1](#0-0) 

`ENABLED_SCRIPT_HASH_TYPE` permits only `{0, 1, 2, 4}`; value `3` is excluded. [2](#0-1) 

`ScriptHashType::try_from(3u8)` returns `Err` (no matching repr), and `verify_value(3)` returns `false` (`3` is odd and not `1`). [3](#0-2) 

The relay-layer guard `check_data` does validate both lock and type scripts on `CellOutputReader`: [4](#0-3) 

However, `check_data` is invoked only in the P2P relay/synchronizer paths, not inside `NonContextualTransactionVerifier::verify()` or the tx-pool's `non_contextual_verify`: [5](#0-4) 

`NonContextualTransactionVerifier::verify()` is the sole non-contextual gate for both the tx-pool and `NonContextualBlockTxsVerifier`: [6](#0-5) 

The `CellbaseVerifier` in `block_verifier.rs` also checks only the lock script hash_type for cellbase outputs, mirroring the same omission: [7](#0-6) 

**Exploit flow:**
1. Craft a `TransactionView` whose first output has a valid lock script and a type script with `hash_type = 3`.
2. Submit via `send_transaction` RPC — bypasses the relay `check_data` guard entirely.
3. `NonContextualTransactionVerifier::verify()` returns `Ok(())` — type script hash_type is never inspected.
4. Transaction enters the tx-pool.
5. A miner (including the attacker acting as miner) includes the transaction in a block.
6. The miner broadcasts the block; honest nodes receiving it via relay call `check_data` on the block's transactions, find the invalid type-script hash_type, and reject the block.
7. Consensus split: the miner's chain diverges from honest nodes.

Additionally, even without a consensus split, the mined cell is permanently unspendable: any spend attempt triggers type-script execution, which the VM rejects for the invalid hash_type.

## Impact Explanation

The primary in-scope impact is **consensus deviation (Critical, 15001–25000 points)**. A miner node that accepts the transaction via RPC (bypassing `check_data`) and mines it produces a block that honest relay-connected nodes reject, splitting the network. Secondary impact is permanent, irrecoverable locking of CKB capacity in the resulting live cell, constituting concrete economic damage to chain state integrity.

## Likelihood Explanation

Any user with RPC access to a miner node can submit the crafted transaction. Local nodes and many hosted nodes expose RPC by default. The transaction is cheap to construct (minimum cell capacity only). No special keys, hashpower, or network position are required beyond RPC access. A malicious miner can self-trigger the consensus split without any external cooperation.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash_type for every output that carries one, mirroring the existing lock-script check:

```rust
for output in self.transaction.outputs() {
    // existing lock check (unchanged) …
    if let Some(type_script) = output.type_().to_opt() {
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_script.hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err(TransactionError::InvalidScriptHashType {
                hash_type: type_script.hash_type(),
            }.into());
        }
    }
}
```

Also update the struct-level doc comment for `NonContextualTransactionVerifier` to reflect that both lock and type script hash types are checked. [8](#0-7) 

## Proof of Concept

**Unit test plan (mirrors existing `ScriptHashTypeVerifier` tests in `verification/src/tests/transaction_verifier.rs`):**

1. Build a `Script` with `hash_type = 3` (reserved/invalid).
2. Build a `CellOutput` with a valid lock script and the above script as `type_`.
3. Construct a `TransactionView` containing that output.
4. Call `NonContextualTransactionVerifier::new(&tx, &consensus).verify()`.
5. Assert the result is `Ok(())` — demonstrating the gap (pre-fix).
6. After applying the fix, assert the result is `Err(TransactionError::InvalidScriptHashType { … })`.

The existing `check_data` test at lines 133–167 of `util/gen-types/src/extension/tests/check_data.rs` already proves that `output_error2` (type script with `hash_type = 3`) is caught by the relay guard but not by `NonContextualTransactionVerifier`. [9](#0-8)

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

**File:** verification/src/transaction_verifier.rs (L93-102)
```rust
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

**File:** util/gen-types/src/core.rs (L39-41)
```rust
    pub fn verify_value(v: u8) -> bool {
        v.is_multiple_of(2) || v == 1
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

**File:** tx-pool/src/util.rs (L56-63)
```rust
pub(crate) fn non_contextual_verify(
    consensus: &Consensus,
    tx: &TransactionView,
) -> Result<(), Reject> {
    NonContextualTransactionVerifier::new(tx, consensus)
        .verify()
        .map_err(Reject::Verification)?;

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

**File:** util/gen-types/src/extension/tests/check_data.rs (L136-138)
```rust
                let output_error2 = packed::CellOutput::new_builder()
                    .type_(script_opt_error.clone())
                    .build();
```
