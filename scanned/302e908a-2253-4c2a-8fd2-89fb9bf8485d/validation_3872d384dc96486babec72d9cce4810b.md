### Title
`ScriptHashTypeVerifier` Fails to Validate Output Type Script Hash Types, Allowing Disabled Hash Types to Bypass Non-Contextual Verification — (`verification/src/transaction_verifier.rs`)

### Summary

`ScriptHashTypeVerifier::verify` only checks `output.lock().hash_type()` for each output. It never checks `output.type_().hash_type()`. A transaction carrying a consensus-disabled `ScriptHashType` (e.g., `Data3 = 6`) in an output's **type script** passes `NonContextualTransactionVerifier` entirely and proceeds into the contextual verification pipeline before being rejected.

### Finding Description

The verifier loop at lines 796–814 is:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(...);
            }
        } else {
            return Err(...);
        }
    }
    Ok(())
}
``` [1](#0-0) 

Only `output.lock().hash_type()` is inspected. `output.type_()` is never touched. The struct comment at line 70 even documents this incompleteness: *"Check whether output lock hash type within enabled range"* — lock only. [2](#0-1) 

`NonContextualTransactionVerifier` calls `self.script_hash_type.verify()` as its final gate before a transaction is considered non-contextually valid: [3](#0-2) 

A transaction with a valid lock (`Data = 0`) and a disabled type script (`Data3 = 6`) on any output clears this gate and is forwarded to `ContextualTransactionVerifier::verify`, which runs `time_relative`, `capacity`, and then the full `script.verify(max_cycles)` pipeline. [4](#0-3) 

### Impact Explanation

Every such crafted transaction forces the node to perform cell-dep resolution, script grouping, and at minimum the initial dispatch work inside `TransactionScriptsVerifier` before the disabled hash type is caught by `select_version` / `extract_script_and_dep_index`. This work is more expensive than the O(n\_outputs) loop that non-contextual verification is supposed to provide. An attacker can flood the tx-pool submission RPC with a stream of such transactions at negligible cost (no PoW, no valid scripts required), causing disproportionate CPU and I/O load on every receiving node — a realistic network-congestion / resource-exhaustion path.

### Likelihood Explanation

The exploit requires no privilege, no key material, and no hashpower. Any unprivileged party can construct and submit such a transaction via the standard `send_transaction` RPC. The malformed field (`type_().hash_type()`) is freely settable in the transaction builder. The gap is structural and present in every code path that calls `NonContextualTransactionVerifier`.

### Recommendation

Extend `ScriptHashTypeVerifier::verify` to also validate the type script hash type for each output, mirroring the existing lock-script check:

```rust
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
```

Additionally, update the struct-level comment at line 70 to reflect that both lock and type scripts must be covered.

### Proof of Concept

1. Build a transaction with one output whose lock script uses `hash_type = Data (0)` and whose type script uses `hash_type = 6` (Data3, consensus-disabled).
2. Submit via `send_transaction` RPC.
3. Observe that `NonContextualTransactionVerifier::verify` returns `Ok(())` — the `ScriptHashTypeVerifier` loop exits without error because only the lock hash type (`0`) is checked.
4. Observe the transaction enters `ContextualTransactionVerifier` and is only rejected inside the script verifier when it attempts to resolve the type script version.
5. Repeat in a tight loop; measure CPU cycles consumed per submission vs. a transaction rejected at the non-contextual stage. [5](#0-4)

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

**File:** verification/src/transaction_verifier.rs (L787-815)
```rust
pub struct ScriptHashTypeVerifier<'a> {
    transaction: &'a TransactionView,
}

impl<'a> ScriptHashTypeVerifier<'a> {
    pub fn new(transaction: &'a TransactionView) -> Self {
        Self { transaction }
    }

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
}
```
