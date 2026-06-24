Audit Report

## Title
`ScriptHashTypeVerifier` Omits Type Script Hash Type Validation, Enabling Consensus Split Under `assume_valid_targets` — (`File: verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs but only validates `output.lock().hash_type()`, never inspecting `output.type_().hash_type()`. A transaction whose type script carries a `ScriptHashType` value absent from `ENABLED_SCRIPT_HASH_TYPE` (e.g., value `6`) passes `NonContextualTransactionVerifier` without error. Under `assume_valid_targets`, where `Switch::DISABLE_SCRIPT` suppresses contextual script execution but `Switch::DISABLE_NON_CONTEXTUAL` is not set, this incomplete gate is the only hash-type check that runs, allowing a crafted block to be accepted by syncing nodes while being rejected by fully-verifying nodes — a consensus split.

## Finding Description

`ENABLED_SCRIPT_HASH_TYPE` is defined as `{0, 1, 2, 4}` — values `6` and above are not permitted. [1](#0-0) 

`ScriptHashTypeVerifier::verify()` loops over outputs and checks only the lock script: [2](#0-1) 

The `output.type_()` field is never read. An output whose lock script uses `ScriptHashType::Data` (value `0`, enabled) and whose type script uses value `6` (not enabled) passes this verifier entirely.

`ScriptHashTypeVerifier` is the sole consensus-level hash-type gate inside `NonContextualTransactionVerifier`: [3](#0-2) 

`NonContextualTransactionVerifier` is invoked from the tx-pool admission path: [4](#0-3) 

Under `assume_valid_targets`, `verify_block` sets `Switch::DISABLE_SCRIPT` (not `DISABLE_NON_CONTEXTUAL`): [5](#0-4) 

`ContextualTransactionVerifier::verify()` skips script execution when `skip_script_verify` is true: [6](#0-5) 

So under `assume_valid_targets`, the execution path is: `NonContextualTransactionVerifier` runs (type script hash type unchecked) → script execution skipped → block accepted. On a fully-verifying node, the same block reaches the script verifier, which fails to resolve a type script with hash type `6`, and the block is rejected. This is a consensus split.

The `Switch` flag definitions confirm `DISABLE_SCRIPT` and `DISABLE_NON_CONTEXTUAL` are independent bits: [7](#0-6) 

## Impact Explanation

A malicious miner can craft a block containing a transaction whose type script carries a disallowed `hash_type` byte. Nodes syncing under `assume_valid_targets` accept the block (non-contextual check passes, script execution skipped). Fully-verifying nodes reject the same block (script verifier fails on unrecognized hash type). This produces a **consensus deviation** — the highest-severity allowed impact class. Additionally, any transaction with a disallowed type script hash type passes `non_contextual_verify` and enters the tx-pool, consuming resources before being evicted by contextual verification.

## Likelihood Explanation

The tx-pool admission path is reachable by any unprivileged user via RPC or P2P relay — crafting an output with an arbitrary `hash_type` byte requires no special access. The consensus split path additionally requires a miner to include such a transaction in a block (bypassing their own tx-pool's contextual check by manually constructing the block) and a target node to have `assume_valid_targets` configured. `assume_valid_targets` is an operator-level opt-in, but it is a documented and supported feature intended for use during IBD. A new node syncing from scratch with `assume_valid_targets` set to a future block is the realistic victim.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type for every output, mirroring the existing lock-script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock check
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err(TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            }.into());
        }
        // missing type script check — add this block
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
    Ok(())
}
```

The same gap exists in `CellbaseVerifier` in `block_verifier.rs` (lines 135–144), which also only checks the lock script hash type for cellbase outputs. [8](#0-7) 

## Proof of Concept

1. Construct a `TransactionView` with one output: lock script uses `ScriptHashType::Data` (value `0`, enabled); type script uses raw `hash_type` byte `6` (not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Call `NonContextualTransactionVerifier::new(&tx, &consensus).verify()`.
3. Observe `Ok(())` — the disallowed type script hash type is not caught.
4. Confirm `ENABLED_SCRIPT_HASH_TYPE` (`{0, 1, 2, 4}`) does not contain `6`.
5. On a fully-verifying node, pass the same transaction through `ContextualTransactionVerifier` with `skip_script_verify = false` — the script verifier fails to resolve hash type `6`, returning an error.
6. On a node with `assume_valid_targets` set (so `Switch::DISABLE_SCRIPT` is active), the same block passes all verification and is committed — demonstrating the split. [2](#0-1) [1](#0-0)

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

**File:** verification/src/transaction_verifier.rs (L162-172)
```rust
    pub fn verify(&self, max_cycles: Cycle, skip_script_verify: bool) -> Result<Completed, Error> {
        self.time_relative.verify()?;
        self.capacity.verify()?;
        let cycles = if skip_script_verify {
            0
        } else {
            self.script.verify(max_cycles)?
        };
        let fee = self.fee_calculator.transaction_fee()?;
        Ok(Completed { cycles, fee })
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

**File:** chain/src/verify.rs (L215-238)
```rust
        let switch: Switch = switch.unwrap_or_else(|| {
            let mut assume_valid_targets = self.shared.assume_valid_targets();
            match *assume_valid_targets {
                Some(ref mut targets) => {
                    //
                    let block_hash: H256 = Into::<H256>::into(BlockView::hash(block));
                    if targets.first().eq(&Some(&block_hash)) {
                        targets.remove(0);
                        info!("CKB reached one assume_valid_target: 0x{}", block_hash);
                    }

                    if targets.is_empty() {
                        assume_valid_targets.take();
                        info!(
                            "CKB reached all assume_valid_targets, will do full verification now"
                        );
                        Switch::NONE
                    } else {
                        Switch::DISABLE_SCRIPT
                    }
                }
                None => Switch::NONE,
            }
        });
```

**File:** verification/traits/src/lib.rs (L38-42)
```rust
        const DISABLE_NON_CONTEXTUAL    = 0b00100000;

        /// Disable script verification
        const DISABLE_SCRIPT            = 0b01000000;

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
