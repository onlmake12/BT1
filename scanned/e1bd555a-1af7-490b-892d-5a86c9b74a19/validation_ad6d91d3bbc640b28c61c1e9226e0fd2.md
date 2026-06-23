### Title
`ScriptHashTypeVerifier` Only Validates Lock Script Hash Types in Outputs, Leaving Type Script Hash Types Unchecked — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` enforces `ENABLED_SCRIPT_HASH_TYPE` only against the **lock script** of each transaction output. The **type script** hash type of every output is never validated. Any transaction sender can embed a disallowed (or future/deprecated) `ScriptHashType` value inside an output's type script, commit that cell to the chain, and later spend it — fully bypassing the restriction that `ENABLED_SCRIPT_HASH_TYPE` is meant to enforce.

---

### Finding Description

`NonContextualTransactionVerifier` includes a `ScriptHashTypeVerifier` step whose stated purpose is:

> *"Verify that the ScriptHashType of transaction outputs is within the range permitted by the current consensus rules."*

The implementation iterates over outputs and checks only `output.lock().hash_type()`:

```rust
// verification/src/transaction_verifier.rs  L796-L814
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

`output.type_()` is never consulted. A cell output in CKB carries two independent scripts — a lock script and an optional type script — each with its own `hash_type` field. The verifier silently ignores the type script's `hash_type`, leaving a complete bypass path:

1. Craft an output whose **lock script** uses an allowed hash type (passes the check).
2. Attach a **type script** whose `hash_type` is outside `ENABLED_SCRIPT_HASH_TYPE` (never checked).
3. Submit the transaction; it passes `NonContextualTransactionVerifier` and is committed.
4. The committed cell now carries a disallowed hash type in its type script on-chain.
5. Any future transaction that consumes this cell will trigger execution of that type script — with the disallowed hash type — through `TransactionScriptsVerifier`, which applies no such filter.

This is structurally identical to M-11: the "deposit" restriction (lock-script check) is enforced, but the "direct transfer" path (type-script field) is left open, and the downstream execution engine (`TransactionScriptsVerifier`) processes all script groups regardless of hash type.

---

### Impact Explanation

**Scenario A — Pre-hardfork hash type leakage.** If a new `ScriptHashType` variant (e.g., `Data3`) is added to the enum but intentionally excluded from `ENABLED_SCRIPT_HASH_TYPE` until a hardfork activates it, any transaction sender can embed it in a type script today. Nodes that have not yet upgraded may handle the unknown hash type differently during script resolution, causing a **consensus split**.

**Scenario B — Deprecated hash type re-use.** If a hash type is removed from `ENABLED_SCRIPT_HASH_TYPE` to deprecate it, existing cells whose type scripts use that hash type can still be created by new transactions (not just pre-existing cells), because only the lock script is gated. The deprecation is therefore ineffective for type scripts.

**Scenario C — Stuck / unspendable cells.** If the script execution engine cannot resolve a code cell referenced by an unsupported hash type, the output becomes permanently unspendable, constituting a capacity-loss DoS for the cell owner.

In all scenarios the attacker is an ordinary, unprivileged transaction sender. No special role or key is required.

---

### Likelihood Explanation

The bypass requires only the ability to submit a standard transaction with a crafted type script field — a capability available to every RPC caller or P2P transaction relayer. The `NonContextualTransactionVerifier` is the first gate; once it passes, no later stage re-checks hash types. Likelihood is **medium-high** for Scenario A whenever a hardfork is in progress, and **medium** for Scenarios B and C at any time.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to validate the type script hash type of every output in addition to the lock script hash type:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // Check lock script hash type (existing)
        let lock_val: u8 = TryInto::<ScriptHashType>::try_into(output.lock().hash_type())
            .map_err(|_| TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            })?
            .into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&lock_val) {
            return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: lock_val }.into());
        }

        // Check type script hash type (missing today)
        if let Some(type_script) = output.type_().to_opt() {
            let type_val: u8 = TryInto::<ScriptHashType>::try_into(type_script.hash_type())
                .map_err(|_| TransactionError::InvalidScriptHashType {
                    hash_type: type_script.hash_type(),
                })?
                .into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&type_val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: type_val }.into());
            }
        }
    }
    Ok(())
}
```

This mirrors the M-11 fix recommendation: split the validation so that both paths (lock and type) are subject to the same "supported list" gate, preventing any new borrowing/use of a disallowed hash type without unfairly invalidating already-committed cells.

---

### Proof of Concept

1. Observe that `ENABLED_SCRIPT_HASH_TYPE` excludes some value `X` (e.g., a future `Data3 = 4`).
2. Construct a `TransactionView` with one output:
   - `lock`: `{ code_hash: <any>, hash_type: Data (0) }` — passes the verifier.
   - `type`: `{ code_hash: <any>, hash_type: X }` — **never checked**.
3. Submit via RPC (`send_transaction`) or P2P relay.
4. `NonContextualTransactionVerifier::verify()` calls `ScriptHashTypeVerifier::verify()`, which iterates outputs, reads only `output.lock().hash_type() = 0`, finds it in `ENABLED_SCRIPT_HASH_TYPE`, and returns `Ok(())`.
5. The transaction is accepted into the tx-pool and eventually committed.
6. The resulting cell carries hash type `X` in its type script on-chain, reachable by any future spending transaction.

**Root cause line:** [1](#0-0) 

**Verifier registration (non-contextual path):** [2](#0-1)

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

**File:** verification/src/transaction_verifier.rs (L797-814)
```rust
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
