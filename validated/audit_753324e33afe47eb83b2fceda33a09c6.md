### Title
Incomplete `ScriptHashType` Enforcement — Type Scripts of Outputs Not Validated — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` enforces the `ENABLED_SCRIPT_HASH_TYPE` consensus rule only against the **lock script** of each output. The **type script** of each output is never checked. An unprivileged transaction sender can craft an output whose type script carries a hash type that is not yet permitted by consensus, bypassing the gating rule entirely.

---

### Finding Description

`ScriptHashTypeVerifier` is part of `NonContextualTransactionVerifier` and is intended to enforce the rule that every script hash type used in transaction outputs must be within the set permitted by the current consensus rules (`ENABLED_SCRIPT_HASH_TYPE`).

The implementation iterates over all outputs and checks only `output.lock().hash_type()`:

```rust
// verification/src/transaction_verifier.rs  lines 796–815
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

`output.type_()` — the optional type script — is never inspected. A transaction output that carries a type script with a hash type outside `ENABLED_SCRIPT_HASH_TYPE` (e.g., a future `Data2 = 4` value before it is activated) passes this verifier without error.

The analog to the reported LPManager bug is exact:

| LPManager | CKB |
|---|---|
| Minimum LP share checked on `removeLiquidity` and on other-user deposits | `ENABLED_SCRIPT_HASH_TYPE` checked on lock scripts of outputs |
| Not checked on market-maker LP token transfers | Not checked on **type scripts** of outputs |
| Market maker bypasses minimum share via transfer | Tx sender bypasses hash-type gate via type script |

---

### Impact Explanation

`ScriptHashTypeVerifier` is composed into `NonContextualTransactionVerifier`, which is called at tx-pool admission (`non_contextual_verify`) and again during block verification. A transaction with a non-permitted hash type in a type script will pass both the pool admission check and the non-contextual block check. If the script execution engine accepts the hash type at runtime (e.g., because the VM already supports it but the consensus gate has not yet been activated), the transaction can be mined into a block, violating the intended phased activation of new script hash types. This undermines the consensus-level feature-gating mechanism that `ENABLED_SCRIPT_HASH_TYPE` is designed to enforce.

---

### Likelihood Explanation

Any unprivileged transaction sender can construct such a transaction. No special role, key, or majority hash power is required. The entry path is the standard RPC `send_transaction` call or P2P relay. The bypass is deterministic and requires only knowledge of the missing check.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the hash type of the type script when it is present:

```rust
// After checking output.lock().hash_type(), also check:
if let Some(type_script) = output.type_().to_opt() {
    if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_script.hash_type()) {
        let val: u8 = hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(
                TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into(),
            );
        }
    } else {
        return Err((TransactionError::InvalidScriptHashType {
            hash_type: type_script.hash_type(),
        }).into());
    }
}
```

This mirrors the existing lock-script check and closes the bypass path for type scripts.

---

### Proof of Concept

1. Identify a `ScriptHashType` value that is not in `ENABLED_SCRIPT_HASH_TYPE` (e.g., `Data2 = 4` if not yet activated).
2. Construct a `TransactionView` whose output has:
   - A valid lock script with a permitted hash type (passes the existing check).
   - A type script with `hash_type = 4` (not in `ENABLED_SCRIPT_HASH_TYPE`).
3. Submit via `send_transaction` RPC or P2P relay.
4. Observe that `NonContextualTransactionVerifier::verify()` returns `Ok(())` — the transaction is admitted to the pool and relayed, bypassing the consensus hash-type gate.

**Root cause location:** [1](#0-0) 

**Verifier composition (where the incomplete check is invoked):** [2](#0-1)

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

**File:** verification/src/transaction_verifier.rs (L796-815)
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
}
```
