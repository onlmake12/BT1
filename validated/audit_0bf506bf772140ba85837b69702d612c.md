### Title
Missing Type Script Hash Type Validation in `ScriptHashTypeVerifier` - (File: `verification/src/transaction_verifier.rs`)

### Summary
`ScriptHashTypeVerifier::verify()` checks the `hash_type` field of each output's **lock** script against `ENABLED_SCRIPT_HASH_TYPE`, but never checks the `hash_type` field of each output's **type** script. A transaction sender can craft outputs whose type scripts carry a disallowed `ScriptHashType` value, bypassing the non-contextual gate entirely.

### Finding Description
`ScriptHashTypeVerifier` is part of `NonContextualTransactionVerifier` and is intended to enforce that only consensus-permitted `ScriptHashType` values appear in transaction outputs. The implementation iterates over outputs and validates only `output.lock().hash_type()`: [1](#0-0) 

The type script slot — accessed via `output.type_().to_opt()` — is never inspected. Each CKB cell output carries two independent script fields (`lock` and `type`), both of which can carry an arbitrary `hash_type` byte. The check for the lock script has no equivalent check for the type script, directly mirroring the "missing approval path" pattern: one code path (lock) is validated, the equivalent code path (type) is not.

Compare with `cell_uses_dao_type_script`, which correctly reads `output.type_()` when it needs to inspect the type script: [2](#0-1) 

### Impact Explanation
An unprivileged transaction sender can submit a transaction whose outputs contain type scripts with a `hash_type` value outside `ENABLED_SCRIPT_HASH_TYPE`. This transaction passes `NonContextualTransactionVerifier` and enters the tx pool. If the script execution layer handles the disallowed hash type differently across node versions or configurations (e.g., one node silently ignores it, another panics or rejects), the result is a consensus split. Even without a split, the transaction occupies pool resources under false pretenses and may be relayed to peers, amplifying the effect.

### Likelihood Explanation
Any RPC caller or P2P peer can submit a crafted transaction. No privilege, key, or majority hash power is required. The attacker only needs to set a non-zero `hash_type` byte in a type script field of any output. The entry path is `process_tx` / `submit_remote_tx` → `non_contextual_verify` → `NonContextualTransactionVerifier::verify()` → `ScriptHashTypeVerifier::verify()`, which returns `Ok(())` without ever inspecting the type script. [3](#0-2) 

### Recommendation
Extend `ScriptHashTypeVerifier::verify()` to also validate the `hash_type` of each output's type script when one is present:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock check
        check_hash_type(output.lock().hash_type())?;
        // missing type check
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}
```

where `check_hash_type` encapsulates the existing `TryInto` + `ENABLED_SCRIPT_HASH_TYPE` logic.

### Proof of Concept
1. Construct a `TransactionView` with one output whose `lock` script uses `ScriptHashType::Data` (permitted) and whose `type` script uses a raw `hash_type` byte value not present in `ENABLED_SCRIPT_HASH_TYPE` (e.g., `0x04`).
2. Call `NonContextualTransactionVerifier::new(&tx, &consensus).verify()`.
3. Observe `Ok(())` — the verifier returns success despite the disallowed type script hash type.
4. Submit the transaction via the `send_transaction` RPC; it enters the pool without rejection at the non-contextual stage. [4](#0-3)

### Citations

**File:** verification/src/transaction_verifier.rs (L80-102)
```rust
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

**File:** verification/src/transaction_verifier.rs (L525-534)
```rust
fn cell_uses_dao_type_script(cell_output: &CellOutput, dao_type_hash: &Byte32) -> bool {
    cell_output
        .type_()
        .to_opt()
        .map(|t| {
            Into::<u8>::into(t.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
                && &t.code_hash() == dao_type_hash
        })
        .unwrap_or(false)
}
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
