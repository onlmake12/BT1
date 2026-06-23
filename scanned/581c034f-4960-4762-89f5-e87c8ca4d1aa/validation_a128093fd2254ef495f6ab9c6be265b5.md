### Title
`ScriptHashTypeVerifier` Checks Only Lock Script Hash Type, Skipping Type Script — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates the `hash_type` of each output's **lock script** against `ENABLED_SCRIPT_HASH_TYPE`, but completely omits the same check for each output's **type script**. This is a direct structural analog to the reported "incomplete opcode check" pattern: a guard function that is supposed to block all disallowed operation types only checks a subset of them.

---

### Finding Description

`ScriptHashTypeVerifier` is part of `NonContextualTransactionVerifier`, the first gate applied to every transaction entering the tx-pool and every block transaction during non-contextual block verification.

The verifier's intent, as stated in its doc comment, is:

> *"Verify that the ScriptHashType of transaction outputs is within the range permitted by the current consensus rules."*

`ENABLED_SCRIPT_HASH_TYPE` is the consensus-controlled allowlist:

```
{ 0 = Data, 1 = Type, 2 = Data1, 4 = Data2 }
```

The actual implementation:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(...ScriptHashTypeNotPermitted...);
            }
        } else {
            return Err(...InvalidScriptHashType...);
        }
        // ← output.type_() is never examined
    }
    Ok(())
}
```

Only `output.lock().hash_type()` is checked. `output.type_()` — the optional type script — is never inspected. A transaction output carrying a type script whose `hash_type` byte is a valid molecule-level enum variant but **not** in `ENABLED_SCRIPT_HASH_TYPE` will silently pass this verifier.

For contrast, the lower-level `check_data` function (which only validates structural validity, not consensus enablement) correctly checks **both** scripts:

```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
```

`check_data` and `ScriptHashTypeVerifier` serve different purposes: `check_data` rejects unknown byte values; `ScriptHashTypeVerifier` rejects hash types that are structurally valid but not yet enabled by consensus. The gap between the two is exactly where the missing type-script check lives.

---

### Impact Explanation

Any transaction sender (RPC `send_transaction`, P2P relay) can craft a transaction whose output contains a type script with a `hash_type` value that is a valid `ScriptHashType` enum variant but absent from `ENABLED_SCRIPT_HASH_TYPE`. Such a transaction bypasses the non-contextual gate and enters the tx-pool. If a miner includes it in a block, the block passes non-contextual block verification (`NonContextualBlockTxsVerifier` calls `NonContextualTransactionVerifier` for each tx). The disallowed hash type is only encountered at script-execution time, where different node versions or implementations may handle it differently, creating a potential consensus split. Additionally, the missing check wastes verification resources on transactions that should have been rejected cheaply at the admission stage.

---

### Likelihood Explanation

The current `ScriptHashType` enum has exactly four variants (0, 1, 2, 4), all of which are in `ENABLED_SCRIPT_HASH_TYPE`. Value 3 is not a valid enum variant and is rejected by `check_data` before reaching `ScriptHashTypeVerifier`. Therefore, **no currently-valid byte value exploits this gap today**. However, `ENABLED_SCRIPT_HASH_TYPE` exists precisely to be a forward-compatible consensus gate: when a new hash type (e.g., `Data3 = 5`) is introduced as a valid enum variant in a future release but not yet activated by consensus, any transaction sender can immediately exploit the missing type-script check to inject transactions with that hash type into the pool and into blocks before the hardfork activates it. The likelihood is **medium** (requires a new hash type to be added to the enum before the fix is applied) but the structural bug is present today.

---

### Recommendation

In `ScriptHashTypeVerifier::verify()`, after checking `output.lock()`, also check `output.type_()` if it is present:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock check ...
        check_hash_type(output.lock().hash_type())?;

        // add type script check
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}
```

Apply the same fix to the analogous check in `CellbaseVerifier` in `verification/src/block_verifier.rs` (lines 135–144), which also only checks `output.lock().hash_type()` for cellbase outputs.

---

### Proof of Concept

1. Construct a `TransactionView` with one output whose **lock** script uses `hash_type = 0` (Data, valid and enabled) and whose **type** script uses `hash_type = 5` (hypothetical future variant, valid enum but not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Call `ScriptHashTypeVerifier::new(&tx).verify()`.
3. Observe: the verifier returns `Ok(())` — the disallowed type-script hash type is not detected.
4. Submit via RPC `send_transaction`; the transaction enters the tx-pool without rejection.
5. A miner calling `get_block_template` will include it; `NonContextualBlockTxsVerifier` passes it for the same reason.

The missing check is at: [1](#0-0) 

The `ENABLED_SCRIPT_HASH_TYPE` allowlist being enforced only for lock scripts: [2](#0-1) 

The analogous gap in `CellbaseVerifier`: [3](#0-2) 

The correct pattern (checking both lock and type) already used in `check_data`: [4](#0-3)

### Citations

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
