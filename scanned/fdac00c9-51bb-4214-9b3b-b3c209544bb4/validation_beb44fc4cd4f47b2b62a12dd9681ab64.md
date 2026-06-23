### Title
`ScriptHashTypeVerifier` Checks Lock Script Hash Type But Omits Type Script Hash Type — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` in `verification/src/transaction_verifier.rs` iterates over transaction outputs and validates the lock script's `hash_type` against `ENABLED_SCRIPT_HASH_TYPE`, but performs no equivalent check on the type script's `hash_type`. This mirrors the external report's pattern exactly: one related flag is checked while the other is silently skipped, allowing a non-permitted hash type to enter the chain via the type script slot.

---

### Finding Description

The struct `ScriptHashTypeVerifier` is documented as:

> "Verify that the ScriptHashType of transaction outputs is within the range permitted by the current consensus rules."

Its `verify()` implementation does this for the lock script only:

```rust
// verification/src/transaction_verifier.rs  L796–L815
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
        // ← No equivalent check for output.type_().to_opt()
    }
    Ok(())
}
```

Every CKB output can carry both a lock script and an optional type script. The lock script's `hash_type` is validated; the type script's `hash_type` is never inspected here. A transaction author can therefore set the type script's `hash_type` to any byte value — including values outside `ENABLED_SCRIPT_HASH_TYPE` — and this non-contextual gate will pass silently. [1](#0-0) 

The constant being enforced is imported from:

```rust
use ckb_constant::consensus::ENABLED_SCRIPT_HASH_TYPE;
``` [2](#0-1) 

`ScriptHashTypeVerifier` is composed into `NonContextualTransactionVerifier`, which is the earliest rejection gate applied to every incoming transaction: [3](#0-2) 

---

### Impact Explanation

A transaction with a type script carrying a non-permitted `hash_type` byte passes `NonContextualTransactionVerifier` and enters the tx-pool. When the block containing it is validated contextually, the CKB-VM encounters a `hash_type` value it was not designed to handle at the time the consensus rule was written. Depending on VM version and hardfork state, this can produce:

- **Consensus split**: nodes running different VM versions may accept or reject the block differently, causing a chain fork.
- **Undefined script resolution**: an unrecognised `hash_type` may cause the VM to resolve the script code incorrectly or skip execution, effectively bypassing the type script's intended enforcement (e.g., a DAO or UDT type script).
- **Premature activation of reserved hash types**: future hash type values reserved for upcoming hardforks can be used before the hardfork activates, producing behaviour that diverges from the intended upgrade path.

---

### Likelihood Explanation

Any unprivileged transaction sender reachable via the `send_transaction` RPC or P2P relay can craft such a transaction. No special privilege, key, or majority hashpower is required. The attacker only needs to set one byte in the type script field of a transaction output to a value outside the permitted set. The non-contextual check that should block this is the one that is missing.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's `hash_type` when a type script is present:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock script check
        let lock_ht = output.lock().hash_type();
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(lock_ht) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err(TransactionError::InvalidScriptHashType { hash_type: lock_ht }.into());
        }

        // NEW: type script check
        if let Some(type_script) = output.type_().to_opt() {
            let type_ht = type_script.hash_type();
            if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_ht) {
                let val: u8 = hash_type.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
                }
            } else {
                return Err(TransactionError::InvalidScriptHashType { hash_type: type_ht }.into());
            }
        }
    }
    Ok(())
}
```

---

### Proof of Concept

1. Craft a transaction whose output has a valid lock script and a type script with `hash_type = 0x03` (or any byte not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Submit via `send_transaction` RPC.
3. `NonContextualTransactionVerifier::verify()` calls `ScriptHashTypeVerifier::verify()`, which only inspects `output.lock().hash_type()` — the type script byte is never read.
4. The transaction passes non-contextual verification and enters the tx-pool.
5. A miner includes it in a block; contextual script execution encounters the unrecognised `hash_type`, producing node-divergent or undefined behaviour. [4](#0-3)

### Citations

**File:** verification/src/transaction_verifier.rs (L5-5)
```rust
use ckb_constant::consensus::ENABLED_SCRIPT_HASH_TYPE;
```

**File:** verification/src/transaction_verifier.rs (L71-103)
```rust
pub struct NonContextualTransactionVerifier<'a> {
    pub(crate) version: VersionVerifier<'a>,
    pub(crate) size: SizeVerifier<'a>,
    pub(crate) empty: EmptyVerifier<'a>,
    pub(crate) duplicate_deps: DuplicateDepsVerifier<'a>,
    pub(crate) outputs_data_verifier: OutputsDataVerifier<'a>,
    pub(crate) script_hash_type: ScriptHashTypeVerifier<'a>,
}

impl<'a> NonContextualTransactionVerifier<'a> {
    /// Creates a new NonContextualTransactionVerifier
    pub fn new(tx: &'a TransactionView, consensus: &'a Consensus) -> Self {
        NonContextualTransactionVerifier {
            version: VersionVerifier::new(tx, consensus.tx_version()),
            size: SizeVerifier::new(tx, consensus.max_block_bytes()),
            empty: EmptyVerifier::new(tx),
            duplicate_deps: DuplicateDepsVerifier::new(tx),
            outputs_data_verifier: OutputsDataVerifier::new(tx),
            script_hash_type: ScriptHashTypeVerifier::new(tx),
        }
    }

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
