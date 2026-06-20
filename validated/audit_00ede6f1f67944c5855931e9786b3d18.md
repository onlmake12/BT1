### Title
Missing `ScriptHashType` Validation for Type Scripts in `ScriptHashTypeVerifier` — (`verification/src/transaction_verifier.rs`)

### Summary

`ScriptHashTypeVerifier`, which is part of the non-contextual (cheap, early) transaction validation pipeline, enforces that output lock scripts use only consensus-permitted `ScriptHashType` values. However, it completely omits the same check for output **type scripts**. A transaction sender can craft an output whose type script carries a disallowed or not-yet-activated hash type, bypass the non-contextual gate, and force the node to proceed to expensive contextual/script-execution verification before the transaction is ultimately rejected.

### Finding Description

`ScriptHashTypeVerifier::verify()` iterates over every transaction output and validates only `output.lock().hash_type()`:

```rust
// verification/src/transaction_verifier.rs  lines 796-815
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

`output.type_()` is never inspected. A type script whose `hash_type` byte is outside `ENABLED_SCRIPT_HASH_TYPE` (e.g., a hash type introduced by a future hard fork that has not yet activated, or a raw byte value that is not a valid `ScriptHashType` enum variant) passes this verifier silently.

The verifier's own doc comment states its intent is to enforce consensus-permitted hash types for transaction outputs — yet it only does so for half of the scripts those outputs may carry. [1](#0-0) 

By contrast, the `CellbaseVerifier` in `block_verifier.rs` avoids the problem for cellbase outputs only because it separately forbids type scripts on cellbase outputs entirely (lines 127–133). That defence does not exist for ordinary transaction outputs. [2](#0-1) 

`ScriptHashTypeVerifier` is composed into `NonContextualTransactionVerifier`, which is the first gate applied to every incoming transaction — from RPC (`send_transaction`), from P2P relay, and from the block assembler path. [3](#0-2) 

### Impact Explanation

1. **Consensus bypass / inconsistency.** The purpose of `ENABLED_SCRIPT_HASH_TYPE` is to enforce which hash types are valid under the current consensus rules. A transaction whose type script carries a disallowed hash type should be rejected at the non-contextual stage on every node. Because the check is absent for type scripts, different nodes may diverge: nodes that reach script execution and encounter the invalid hash type will reject the transaction with a script error, while nodes that short-circuit for other reasons may behave differently. This is a correctness gap in the consensus enforcement layer.

2. **Resource exhaustion / DoS.** The non-contextual check is intentionally cheap. By bypassing it, an attacker forces the node to perform cell resolution, capacity verification, and script execution setup before the transaction is rejected. An attacker submitting a stream of such transactions (valid lock scripts, invalid type script hash type) can consume CPU and memory in the tx-pool verification pipeline disproportionate to the cost of crafting the transactions.

### Likelihood Explanation

The entry path requires no privilege. Any unprivileged actor can:
- Call the `send_transaction` JSON-RPC endpoint, or
- Relay a transaction over the P2P network.

Crafting a transaction with a syntactically valid structure but a type script whose `hash_type` byte is outside `ENABLED_SCRIPT_HASH_TYPE` is trivial. No key material, mining power, or insider access is required.

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the hash type of each output's optional type script:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock-script check
        let lock_hash_type = output.lock().hash_type();
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(lock_hash_type) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err(TransactionError::InvalidScriptHashType { hash_type: lock_hash_type }.into());
        }

        // NEW: type-script check
        if let Some(type_script) = output.type_().to_opt() {
            let type_hash_type = type_script.hash_type();
            if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_hash_type) {
                let val: u8 = hash_type.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
                }
            } else {
                return Err(TransactionError::InvalidScriptHashType { hash_type: type_hash_type }.into());
            }
        }
    }
    Ok(())
}
```

### Proof of Concept

1. Construct a `TransactionView` with one output whose lock script uses a valid, enabled hash type (e.g., `Type = 0x01`) and whose type script uses a hash type byte that is not in `ENABLED_SCRIPT_HASH_TYPE` (e.g., a future or reserved value).
2. Submit via `send_transaction` RPC or P2P relay.
3. `NonContextualTransactionVerifier::verify()` calls `ScriptHashTypeVerifier::verify()`, which only inspects `output.lock().hash_type()` — the invalid type-script hash type is never checked, and the verifier returns `Ok(())`.
4. The transaction proceeds to `ContextualTransactionVerifier`, consuming cell resolution and script-execution resources before being rejected at the script execution stage.
5. Repeat at high frequency to exhaust node verification resources. [4](#0-3)

### Citations

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

**File:** verification/src/block_verifier.rs (L127-133)
```rust
        if cellbase_transaction
            .outputs()
            .into_iter()
            .any(|output| output.type_().is_some())
        {
            return Err((CellbaseError::InvalidTypeScript).into());
        }
```
