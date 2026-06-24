Audit Report

## Title
`ScriptHashTypeVerifier` Omits Type Script Hash-Type Check, Enabling Uncommittable Transactions in Tx-Pool — (File: verification/src/transaction_verifier.rs)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates the `hash_type` of each output's **lock** script against `ENABLED_SCRIPT_HASH_TYPE`, but never inspects the **type** script. A transaction submitted via RPC with a type script carrying a consensus-disallowed `hash_type` (e.g. `0x06`) passes all non-contextual checks and is admitted to the tx-pool, yet is permanently unmineable, creating a persistent pool-pollution vector.

## Finding Description

`NonContextualTransactionVerifier` runs `ScriptHashTypeVerifier` as its final gate: [1](#0-0) 

The verifier's loop reads only `output.lock().hash_type()`: [2](#0-1) 

`output.type_()` is never consulted. The struct-level comment at line 785 says "Verify that the ScriptHashType of transaction outputs is within the range permitted by the current consensus rules" — but the implementation silently ignores the type script.

`ENABLED_SCRIPT_HASH_TYPE` is `{0, 1, 2, 4}`: [3](#0-2) 

The relay/sync layer's `check_data()` calls `ScriptHashType::verify_value`, which tests only whether the byte is a known enum discriminant — it does **not** test against `ENABLED_SCRIPT_HASH_TYPE`: [4](#0-3) 

`verify_value` rejects structurally unknown values (e.g. `0x06` is not a defined variant), so the relay path would drop such a transaction. However, the RPC `send_transaction` path feeds directly into `NonContextualTransactionVerifier`, which only runs `ScriptHashTypeVerifier`. Because `ScriptHashTypeVerifier` never reads the type script, a transaction with `type_.hash_type = 0x06` submitted via RPC:

1. Passes `ScriptHashTypeVerifier` (type script unchecked).
2. Enters the tx-pool.
3. Fails permanently at block-validation time when `ContextualTransactionVerifier` runs script execution and `select_version()` rejects the unknown hash type with `InvalidScriptHashType` / `InvalidVmVersion`.
4. The tx-pool slot remains occupied until eviction; the attacker recycles UTXOs and repeats.

## Impact Explanation

This matches **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. An attacker with a small UTXO set can continuously submit transactions whose type scripts carry `hash_type = 0x06`. These transactions occupy tx-pool slots indefinitely. Because the tx-pool marks the referenced inputs as pending, the attacker can recycle UTXOs after eviction and sustain pool pressure, displacing legitimate transactions and degrading throughput for all users.

## Likelihood Explanation

The attack requires only the ability to call `send_transaction` via the public JSON-RPC interface and ownership of any UTXOs. No hashpower, no Sybil capability, and no special privileges are needed. Constructing the malformed transaction is trivial: set any output's type script `hash_type` byte to `0x06`. The attacker's cost is proportional to their UTXO count and the tx-pool eviction interval, both of which are low barriers.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to check the type script's `hash_type` for each output, mirroring the existing lock-script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // Existing lock script check
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

        // Add: type script check
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

Also update the struct-level doc comment and the `NonContextualTransactionVerifier` comment at line 70 to reflect that both lock and type scripts are checked.

## Proof of Concept

1. Obtain any unspent cell on a CKB testnet node.
2. Construct a transaction spending that cell, with one output whose `lock` uses `hash_type = 0x01` (Type, valid) and whose `type_` uses `hash_type = 0x06` (not in `ENABLED_SCRIPT_HASH_TYPE`).
3. Submit via `send_transaction` RPC.
4. **Expected (buggy) result**: the call succeeds; the transaction appears in `get_raw_tx_pool`.
5. Attempt to mine a block containing this transaction.
6. **Expected result**: block validation fails; the transaction is never committed.
7. The tx-pool slot remains occupied. Repeat with additional UTXOs to exhaust pool capacity.

A unit test can be added to `verification/src/tests/transaction_verifier.rs` mirroring the existing `ScriptHashTypeVerifier` tests but setting the type script's `hash_type` to `0x06` and asserting `ScriptHashTypeNotPermitted` is returned — currently this assertion would **fail**, confirming the bug.

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

**File:** util/gen-types/src/extension/check_data.rs (L10-27)
```rust
impl<'r> packed::ScriptReader<'r> {
    fn check_data(&self) -> bool {
        core::ScriptHashType::verify_value(self.hash_type().into())
    }
}

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
