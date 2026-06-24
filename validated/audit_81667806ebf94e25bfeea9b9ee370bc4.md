The code confirms the claim exactly. The `ScriptHashTypeVerifier::verify` loop at lines 796–814 only inspects `output.lock().hash_type()` and never checks `output.type_().hash_type()`. [1](#0-0) 

The struct-level comment at line 70 also confirms this incompleteness, documenting only "Check whether output lock hash type within enabled range." [2](#0-1) 

`NonContextualTransactionVerifier::verify` calls `self.script_hash_type.verify()` as its final gate before returning `Ok(())`. [3](#0-2) 

A transaction with a disabled type script hash type passes this gate and enters `ContextualTransactionVerifier::verify`, which runs `time_relative`, `capacity`, and the full `script.verify(max_cycles)` pipeline. [4](#0-3) 

---

Audit Report

## Title
`ScriptHashTypeVerifier` Omits Type Script Hash Type Validation, Allowing Disabled Hash Types to Bypass Non-Contextual Verification — (File: `verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify` iterates over transaction outputs and validates only the lock script's `hash_type`, never the type script's `hash_type`. A transaction carrying a consensus-disabled `ScriptHashType` in an output's type script passes `NonContextualTransactionVerifier` entirely and is forwarded into the more expensive `ContextualTransactionVerifier` pipeline before being rejected. This asymmetry allows an unprivileged attacker to force disproportionate CPU and I/O work on every receiving node at negligible cost.

## Finding Description

The `ScriptHashTypeVerifier::verify` loop at lines 796–814 of `verification/src/transaction_verifier.rs` reads only `output.lock().hash_type()` in every iteration. `output.type_()` is never touched. The struct-level comment at line 70 explicitly documents this incompleteness: *"Check whether output lock hash type within enabled range"* — confirming only the lock script is in scope.

`NonContextualTransactionVerifier::verify` (lines 94–102) calls `self.script_hash_type.verify()` as its final gate. A transaction with a valid lock (`Data = 0`) and a disabled type script (`Data3 = 6`) on any output clears this gate and is forwarded to `ContextualTransactionVerifier::verify` (lines 162–171), which runs `time_relative.verify()`, `capacity.verify()`, and then the full `script.verify(max_cycles)` pipeline — including cell-dep resolution, script grouping, and initial dispatch inside `TransactionScriptsVerifier` — before the disabled hash type is caught deep in the script execution layer.

The existing check is structurally insufficient: it is a loop over outputs that only reads one of two possible scripts per output, leaving the type script path entirely unchecked.

## Impact Explanation

This matches the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* The non-contextual verifier is designed to be an O(n_outputs) cheap gate that rejects structurally invalid transactions before any expensive work begins. Bypassing it forces cell-dep resolution, script grouping, and script dispatch for every crafted transaction — work that is orders of magnitude more expensive than the skipped loop iteration. An attacker flooding the `send_transaction` RPC with a stream of such transactions causes disproportionate CPU and I/O load on every receiving node.

## Likelihood Explanation

The exploit requires no privilege, no key material, and no hashpower. Any unprivileged party can construct and submit such a transaction via the standard `send_transaction` RPC. The malformed field (`type_().hash_type()`) is freely settable in any transaction builder. The gap is structural and present in every code path that calls `NonContextualTransactionVerifier`. The attack is trivially repeatable in a tight loop.

## Recommendation

Extend `ScriptHashTypeVerifier::verify` to also validate the type script hash type for each output, mirroring the existing lock-script check:

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
            return Err((TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            }).into());
        }
        // add type script check
        if let Some(type_script) = output.type_().to_opt() {
            if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_script.hash_type()) {
                let val: u8 = hash_type.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
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

Also update the struct-level comment at line 70 and the `NonContextualTransactionVerifier` doc comment to reflect that both lock and type scripts are covered.

## Proof of Concept

1. Build a transaction with one output whose lock script uses `hash_type = 0` (Data, enabled) and whose type script uses `hash_type = 6` (Data3, consensus-disabled).
2. Submit via `send_transaction` RPC.
3. Observe that `NonContextualTransactionVerifier::verify` returns `Ok(())` — the `ScriptHashTypeVerifier` loop exits without error because only the lock hash type (`0`) is checked.
4. Observe the transaction enters `ContextualTransactionVerifier` and is only rejected inside the script verifier when it attempts to resolve the type script version.
5. Write a unit test mirroring the existing `ScriptHashTypeVerifier` tests in `verification/src/tests/transaction_verifier.rs` that constructs such a transaction and asserts `NonContextualTransactionVerifier::verify` currently returns `Ok(())` (demonstrating the gap), then apply the fix and assert it returns the expected `ScriptHashTypeNotPermitted` error.

### Citations

**File:** verification/src/transaction_verifier.rs (L70-70)
```rust
/// - Check whether output lock hash type within enabled range
```

**File:** verification/src/transaction_verifier.rs (L94-102)
```rust
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

**File:** verification/src/transaction_verifier.rs (L162-171)
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
