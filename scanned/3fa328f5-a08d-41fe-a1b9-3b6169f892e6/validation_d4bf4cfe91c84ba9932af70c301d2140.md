### Title
`ScriptHashTypeVerifier` Only Validates Lock Script Hash Types, Silently Permitting Non-Enabled Type Script Hash Types Through Non-Contextual Verification — (`verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and checks only the **lock script** hash type against `ENABLED_SCRIPT_HASH_TYPE`. It never inspects the **type script** hash type. A transaction output carrying a type script with a future/non-permitted hash type (e.g., `Data3` = 6, `Data4` = 8, …) passes non-contextual verification silently, mirroring the external report's pattern where `ChainlinkAdapterOracle` only handled single-asset lookups and left derived-asset types completely unvalidated.

---

### Finding Description

`ENABLED_SCRIPT_HASH_TYPE` in `util/constant/src/consensus.rs` permits only `{0, 1, 2, 4}` (Data, Type, Data1, Data2). [1](#0-0) 

`ScriptHashType::verify_value()` accepts any even byte or `1` as structurally valid, meaning `Data3` (6), `Data4` (8), … all parse successfully as `ScriptHashType` variants. [2](#0-1) 

`ScriptHashTypeVerifier::verify()` loops over outputs and calls `output.lock().hash_type()` — it never calls `output.type_().hash_type()`:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(...)
            }
        } else {
            return Err(...)
        }
    }
    Ok(())
}
``` [3](#0-2) 

The same gap exists in `CellbaseVerifier`, which also only checks `output.lock().hash_type()` and never the type script. [4](#0-3) 

`check_data()` in `util/gen-types/src/extension/check_data.rs` does check both lock and type scripts, but only via `verify_value()` (structural validity), not against `ENABLED_SCRIPT_HASH_TYPE`. [5](#0-4) 

The downstream `select_version()` in `script/src/types.rs` does reject `Data3`+ with `ScriptError::InvalidScriptHashType`, but only at contextual (script-execution) time: [6](#0-5) 

This creates a two-tier inconsistency:
- **Lock scripts** with non-permitted hash types → rejected at non-contextual verification (cheap, early).
- **Type scripts** with non-permitted hash types → pass non-contextual verification, only rejected at contextual script execution (expensive, late) — or not rejected at all when `skip_script_verify = true`.

`ContextualTransactionVerifier::verify()` exposes the `skip_script_verify` bypass: [7](#0-6) 

When `skip_script_verify` is `true` (used during IBD/fast-sync), a transaction output with a type script using `Data3` passes **both** non-contextual and contextual verification and is committed to the chain. The resulting live cell is permanently unspendable: any future transaction consuming it will fail at `select_version()` with `InvalidScriptHashType`, locking the CKB capacity forever.

---

### Impact Explanation

1. **Resource exhaustion (always reachable):** Any unprivileged transaction sender can craft outputs with `Data3`+ type scripts. These pass `ScriptHashTypeVerifier` and enter the contextual verification pipeline, wasting CPU/memory on script group construction and VM setup before being rejected.

2. **Permanently frozen capacity (reachable when `skip_script_verify = true`):** During IBD or any node configuration that enables `skip_script_verify`, a block containing such a transaction is accepted. The resulting cell can never be spent — its type script hash type is unresolvable by `select_version()` — permanently destroying the CKB tokens locked in that cell.

---

### Likelihood Explanation

- The resource-exhaustion path is reachable by any RPC/P2P transaction submitter with no privileges.
- The frozen-capacity path requires the transaction to appear in a block accepted under `skip_script_verify`. This is realistic during IBD if a malicious or buggy peer relays such a block, or if a miner deliberately includes such a transaction.
- The `ScriptHashType` enum already defines `Data3`–`Data127` as valid parsed values, so constructing such a transaction requires no special tooling.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type for each output, mirroring the lock-script check:

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

Apply the same fix to `CellbaseVerifier` in `verification/src/block_verifier.rs`.

---

### Proof of Concept

1. Construct a transaction with one output whose **lock** uses `ScriptHashType::Data` (permitted) and whose **type** uses `ScriptHashType::Data3` (not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Run `ScriptHashTypeVerifier::new(&tx).verify()` — it returns `Ok(())` because only the lock script is checked.
3. Run `NonContextualTransactionVerifier::new(&tx, &consensus).verify()` — also `Ok(())`.
4. With `skip_script_verify = true`, run `ContextualTransactionVerifier::verify(max_cycles, true)` — also `Ok(())`, committing the cell.
5. Attempt to spend the committed cell: `select_version()` returns `Err(ScriptError::InvalidScriptHashType(...))` — the cell is permanently frozen.

The existing test `test_not_enabled_hash_type_output_lock` in `verification/src/tests/transaction_verifier.rs` confirms the lock-script path is caught, but no analogous test exists for the type-script path, confirming the gap. [8](#0-7)

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

**File:** util/gen-types/src/extension/check_data.rs (L24-28)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
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
