### Title
Incomplete `ScriptHashTypeVerifier` Checks Only Output Lock Scripts, Omitting Output Type Scripts — (File: `verification/src/transaction_verifier.rs`)

### Summary

`ScriptHashTypeVerifier::verify()` enforces that output lock scripts use only consensus-permitted `hash_type` values, but performs no equivalent check on output **type scripts**. This is structurally analogous to the BSB22 blinding inconsistency: a guard that is supposed to be applied uniformly across all script slots in an output is only applied to one of the two slots. A transaction sender can embed a type script carrying a disallowed or future `hash_type` byte into any output, bypass the non-contextual gate, and commit that cell to the canonical chain.

---

### Finding Description

`ScriptHashTypeVerifier` is the non-contextual firewall that prevents outputs with invalid or not-yet-activated `ScriptHashType` values from entering the chain. Its `verify()` method iterates over every output and validates `output.lock().hash_type()`: [1](#0-0) 

The loop body calls `output.lock().hash_type()` and checks it against `ENABLED_SCRIPT_HASH_TYPE`. It never touches `output.type_()`. An output that carries a type script with `hash_type = 0x03` (or any other byte outside `ENABLED_SCRIPT_HASH_TYPE`) passes this verifier without error, because the type-script branch is simply absent.

The verifier is wired into `NonContextualTransactionVerifier`, which is the first gate applied to every incoming transaction: [2](#0-1) 

`NonContextualBlockTxsVerifier` applies the same path to every transaction in a received block: [3](#0-2) 

Because the type-script slot is never validated here, a crafted output with a forbidden `hash_type` in its type script clears all non-contextual checks and is committed to the chain.

The parallel in the cellbase path is harmless only because `CellbaseVerifier` already forbids type scripts on cellbase outputs entirely: [4](#0-3) 

For ordinary transactions there is no such blanket prohibition, so the gap in `ScriptHashTypeVerifier` is the only guard, and it is incomplete.

---

### Impact Explanation

1. **Permanent fund lock.** A cell committed with a type script whose `hash_type` byte is not in `ENABLED_SCRIPT_HASH_TYPE` cannot be spent: the script verifier will fail to resolve the script group at spend time. The capacity is irrecoverably locked.

2. **Pre-hardfork cell poisoning.** If a future hardfork activates a new `hash_type` value, cells created before the hardfork with that byte in their type scripts become spendable under the new semantics. Because those cells bypassed the non-contextual gate, they were never subject to any consensus review at creation time. This creates an uncontrolled class of pre-existing cells that activate on hardfork without the usual validation path.

3. **Inconsistent chain state.** The invariant "every output on the canonical chain has only permitted hash types in all its scripts" is broken for type scripts, even though it holds for lock scripts. Downstream tooling, indexers, and light clients that assume this invariant may misbehave.

---

### Likelihood Explanation

The entry path requires only a standard transaction submission (RPC `send_transaction` or P2P relay). No special privilege, key material, or majority hash power is needed. Any unprivileged transaction sender can craft the malformed output. The check that should catch it (`ScriptHashTypeVerifier`) is present but structurally incomplete, so the bypass is deterministic, not probabilistic.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the `hash_type` of the type script on every output that carries one:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock-script check
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

        // NEW: type-script check
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

---

### Proof of Concept

Construct a transaction whose single output has:
- a valid lock script (`hash_type = Data`, value `0x00`)
- a type script with `hash_type = 0x03` (not in `ENABLED_SCRIPT_HASH_TYPE`)

Submit via `send_transaction` RPC or relay over P2P.

`ScriptHashTypeVerifier::verify()` iterates the output, checks `output.lock().hash_type()` → `0x00` → passes. It never reads `output.type_().hash_type()`. The transaction clears `NonContextualTransactionVerifier`, passes contextual checks (the type script is not executed at admission time because the output cell is being *created*, not consumed), and is committed to the chain. The capacity in that output is now permanently inaccessible because any future spend attempt will fail when the script verifier tries to resolve a script group with an unsupported `hash_type`. [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** verification/src/transaction_verifier.rs (L71-102)
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

**File:** verification/src/block_verifier.rs (L269-286)
```rust
pub struct NonContextualBlockTxsVerifier<'a> {
    consensus: &'a Consensus,
}

impl<'a> NonContextualBlockTxsVerifier<'a> {
    /// Creates a new NonContextualBlockTxsVerifier
    pub fn new(consensus: &'a Consensus) -> Self {
        NonContextualBlockTxsVerifier { consensus }
    }

    /// Perform context-independent verification checks for block transactions
    pub fn verify(&self, block: &BlockView) -> Result<Vec<()>, Error> {
        block
            .transactions()
            .iter()
            .map(|tx| NonContextualTransactionVerifier::new(tx, self.consensus).verify())
            .collect()
    }
```
