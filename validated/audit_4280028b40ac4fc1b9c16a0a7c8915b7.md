### Title
Incomplete Script Hash Type Validation in `ScriptHashTypeVerifier` Omits Type Script Check — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` only validates the `hash_type` field of a cell output's **lock** script, but every CKB cell output can also carry an optional **type** script whose `hash_type` is never checked. A transaction sender can craft outputs whose type script carries an invalid or consensus-unsupported `hash_type` byte, bypass the non-contextual gate entirely, and have the transaction admitted to the tx-pool and relayed across the P2P network.

---

### Finding Description

`NonContextualTransactionVerifier` is the first line of defence applied to every submitted transaction. It runs six sub-verifiers in sequence:

```
version → size → empty → duplicate_deps → outputs_data → script_hash_type
``` [1](#0-0) 

The last sub-verifier, `ScriptHashTypeVerifier`, is documented as verifying "that the ScriptHashType of transaction outputs is within the range permitted by the current consensus rules." Its implementation iterates over every output and checks **only** `output.lock().hash_type()`:

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
``` [2](#0-1) 

A CKB cell output carries two scripts: a mandatory `lock` and an optional `type_`. The verifier never calls `output.type_().to_opt()` and therefore never inspects the type script's `hash_type`. An output whose type script contains:

- a raw byte that cannot be decoded into any `ScriptHashType` variant (e.g., `0xFF`), or
- a valid variant that is not present in `ENABLED_SCRIPT_HASH_TYPE` (a future or disabled hash type),

passes `ScriptHashTypeVerifier` without error.

The analog to the external report is exact: `addPool` checked `LENDING_POOL_ADDRESSES_PROVIDER` and `BAD_DEBT_MANAGER` but omitted `LENDING_POOL` and `LENDING_POOL_CONFIGURATOR`. Here, `ScriptHashTypeVerifier` checks the lock script hash type but omits the type script hash type, even though both are equally required to be valid by consensus.

---

### Impact Explanation

A transaction carrying outputs with an invalid or unsupported type script `hash_type`:

1. **Passes non-contextual verification** — admitted to the local tx-pool.
2. **Is relayed to all connected peers** via the compact-block relay and transaction relay protocols, propagating the invalid transaction network-wide.
3. **Consumes tx-pool memory and relay bandwidth** on every node that receives it.
4. **Fails only at script execution time** (contextual verification), wasting CPU cycles on every node that attempts to include it in a block template.
5. **Cannot be mined into a valid block**, but the early-rejection gate that is supposed to prevent such transactions from entering the pool is silently bypassed.

The net effect is a resource-exhaustion / DoS vector reachable by any unprivileged RPC caller or P2P transaction sender: craft a batch of transactions with invalid type script hash types, submit them, and cause every peer to waste pool memory, relay bandwidth, and script-execution cycles before eventually discarding them.

---

### Likelihood Explanation

- **Entry path is fully unprivileged**: any node's `send_raw_transaction` RPC endpoint or the P2P transaction relay protocol accepts the transaction.
- **Trivial to craft**: setting a single byte in the type script's `hash_type` field to `0xFF` (or any value outside `ENABLED_SCRIPT_HASH_TYPE`) is sufficient.
- **No special knowledge required**: the CKB serialization format is public and well-documented.
- **No on-chain cost**: the transaction is never mined, so the attacker pays no fees.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the `hash_type` of each output's optional type script:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock check
        check_hash_type(output.lock().hash_type())?;

        // missing type script check
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}
```

where `check_hash_type` encapsulates the existing `TryInto<ScriptHashType>` conversion and `ENABLED_SCRIPT_HASH_TYPE` membership test. This mirrors the completeness fix applied in the referenced pull request and ensures that all script hash types present in a transaction output are validated at the earliest possible stage. [3](#0-2) 

---

### Proof of Concept

1. Build a `TransactionView` with one output whose `lock` script has a valid, permitted `hash_type` (e.g., `0x01` = `Type`) and whose `type_` script has `hash_type = 0xFF` (not a valid `ScriptHashType`).
2. Call `NonContextualTransactionVerifier::new(&tx, &consensus).verify()`.
3. Observe that the call returns `Ok(())` — the transaction passes all six non-contextual checks.
4. Submit the transaction via `send_raw_transaction` RPC; it is accepted into the tx-pool.
5. Observe the transaction being relayed to peers and consuming pool memory until contextual verification (script execution) eventually rejects it.

The root cause is confirmed at: [4](#0-3) 

where `output.type_()` is never accessed, leaving the type script `hash_type` entirely unchecked.

### Citations

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

**File:** verification/src/transaction_verifier.rs (L785-815)
```rust
// Verify that the ScriptHashType of transaction outputs
// is within the range permitted by the current consensus rules.
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
