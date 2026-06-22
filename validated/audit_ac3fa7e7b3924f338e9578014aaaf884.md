### Title
Incomplete `ScriptHashType` Enforcement: Type Scripts in Outputs Are Not Validated — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` enforces the `ENABLED_SCRIPT_HASH_TYPE` consensus restriction only on the **lock script** of each output cell. It never inspects the **type script** of the same output. An unprivileged transaction sender can therefore create on-chain cells whose type scripts carry a disallowed or reserved `hash_type` value, bypassing the intended consensus gate entirely.

---

### Finding Description

`NonContextualTransactionVerifier` runs `ScriptHashTypeVerifier` as one of its mandatory checks before a transaction is admitted to the tx-pool or committed to a block. [1](#0-0) 

The verifier iterates over every output and reads `output.lock().hash_type()`, validates it against `ENABLED_SCRIPT_HASH_TYPE`, and returns an error if the value is not permitted. The type script of the same output — `output.type_()` — is never examined:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(...)
            }
        } else { ... }
        // ← output.type_() is never checked here
    }
    Ok(())
}
``` [2](#0-1) 

The `ENABLED_SCRIPT_HASH_TYPE` constant is a consensus-level allowlist that controls which `ScriptHashType` variants nodes will accept. Its purpose is to prevent use of hash types that are reserved for future protocol versions. [3](#0-2) 

The analogy to the TokenCard report is exact: just as `executeTransaction` checked `transfer` and `approve` but missed `increaseApproval` (an equivalent operation), `ScriptHashTypeVerifier` checks lock-script hash types but misses type-script hash types — an equivalent field on the same cell output.

---

### Impact Explanation

A transaction author can craft an output cell whose **type script** uses a `hash_type` byte that is not in `ENABLED_SCRIPT_HASH_TYPE` (e.g., a reserved future value such as `Data2 = 4` or any raw byte not yet defined). The `NonContextualTransactionVerifier` passes, the transaction enters the tx-pool, and miners can include it in a block.

Consequences:

1. **Consensus rule bypass**: The `ENABLED_SCRIPT_HASH_TYPE` gate — a consensus invariant — is silently circumvented for type scripts. Cells with forbidden type-script hash types are committed on-chain.
2. **Permanent fund lock / loss**: When the cell owner later tries to spend the cell, the script verifier attempts to resolve and execute the type script. Because the hash type is unrecognised, resolution fails and the cell becomes permanently unspendable, destroying the capacity locked in it.
3. **Future upgrade hazard**: If a future hard fork assigns semantics to the reserved hash-type byte, pre-existing cells created with that byte under the current (unchecked) regime may behave in unintended ways, creating a consensus inconsistency between old and new nodes. [4](#0-3) 

---

### Likelihood Explanation

The entry path requires no privilege: any RPC caller (`send_transaction`) or P2P relayer can submit a transaction. The `NonContextualTransactionVerifier` is the first gate and runs on every submitted transaction. [5](#0-4) 

Constructing such a transaction is trivial — set the `hash_type` byte of a type script to any value outside `ENABLED_SCRIPT_HASH_TYPE`. No key material, no miner collusion, and no Sybil capability is required.

---

### Recommendation

**Short term:** Extend `ScriptHashTypeVerifier::verify()` to also validate `output.type_()` when a type script is present:

```rust
// after the existing lock-script check, add:
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

**Long term:** Audit all other per-field consensus checks to confirm they are applied symmetrically to both lock and type scripts in every position (inputs, outputs, cell deps).

---

### Proof of Concept

1. Build a `TransactionView` with one output whose **lock script** uses `ScriptHashType::Data` (permitted) and whose **type script** uses raw `hash_type = 0x04` (reserved, not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Submit via `send_transaction` RPC or relay over P2P.
3. `NonContextualTransactionVerifier::verify()` calls `ScriptHashTypeVerifier::verify()`, which iterates outputs, reads only `output.lock().hash_type()` → `Data` → permitted → no error returned.
4. The transaction passes all non-contextual checks and enters the tx-pool.
5. A miner assembles a block containing the transaction; the block is accepted by all peers running the same code.
6. The output cell is now on-chain with a type script carrying a forbidden `hash_type`. Any subsequent spend attempt that triggers the type script will fail at script resolution, permanently locking the capacity. [1](#0-0) [5](#0-4)

### Citations

**File:** verification/src/transaction_verifier.rs (L1-6)
```rust
use crate::cache::Completed;
use crate::error::TransactionErrorSource;
use crate::{TransactionError, TxVerifyEnv};
use ckb_chain_spec::consensus::Consensus;
use ckb_constant::consensus::ENABLED_SCRIPT_HASH_TYPE;
use ckb_dao::DaoCalculator;
```

**File:** verification/src/transaction_verifier.rs (L80-103)
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
