### Title
Type Script `hash_type` Not Validated in `ScriptHashTypeVerifier`, Allowing Unpermitted Hash Types to Bypass Non-Contextual Consensus Check — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` validates the `hash_type` field of every output's **lock script**, but entirely omits the same check for the **type script**. Because CKB cells may carry both a lock and an optional type script, a transaction submitter can craft an output whose type script carries a `hash_type` value that is either unknown or explicitly excluded from `ENABLED_SCRIPT_HASH_TYPE`, and that transaction will pass the non-contextual consensus gate without error.

---

### Finding Description

`ScriptHashTypeVerifier` is composed into `NonContextualTransactionVerifier` and is the designated consensus-level gate for rejecting transactions that reference script hash types not yet permitted by the current fork rules. [1](#0-0) 

The loop body reads:

```rust
for output in self.transaction.outputs() {
    if let Ok(hash_type) =
        TryInto::<ScriptHashType>::try_into(output.lock().hash_type())   // ← lock only
    {
        let val: u8 = hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
        }
    } else {
        return Err(TransactionError::InvalidScriptHashType {
            hash_type: output.lock().hash_type(),
        }.into());
    }
}
```

`output.type_()` is never consulted. A type script is optional (`Option`-like via `to_opt()`), but when it is present its `hash_type` byte is subject to the same consensus restrictions as a lock script's. The verifier silently skips it.

The analog to the ERC777 report is exact:

| ERC777 report | CKB analog |
|---|---|
| `withdraw` calls `transfer` (ERC20 interface) on every token | `ScriptHashTypeVerifier` calls `output.lock().hash_type()` on every output |
| ERC777 tokens may not implement `transfer` | Type scripts may carry a `hash_type` not in `ENABLED_SCRIPT_HASH_TYPE` |
| Call silently fails or reverts | Validation silently passes; invalid hash type reaches contextual execution | [2](#0-1) 

The verifier is wired into the non-contextual pipeline here: [3](#0-2) 

---

### Impact Explanation

**Consensus bypass / inconsistent rejection path.** `NonContextualTransactionVerifier` is the early-exit gate that every node applies before touching chain state. Its contract is that any transaction passing it is structurally valid under current consensus rules. By not checking the type script's `hash_type`:

1. A transaction carrying a type script with a `hash_type` byte that is valid as a `ScriptHashType` enum variant but excluded from `ENABLED_SCRIPT_HASH_TYPE` (e.g., a future fork value) passes non-contextual verification on all current nodes.
2. Contextual verification (`TransactionScriptsVerifier`) then attempts to execute the type script. Depending on whether the running node's VM/script layer recognises the hash type, the outcome diverges: nodes that do recognise it may accept the transaction; nodes that do not will reject it. This is a consensus split vector.
3. Even in the uniform-rejection case, the transaction consumes contextual verification resources (script group construction, VM setup) that the non-contextual gate was designed to avoid. [4](#0-3) 

---

### Likelihood Explanation

Any unprivileged RPC caller or P2P transaction relayer can submit a crafted transaction. Constructing an output with an arbitrary `hash_type` byte in its type script requires no special key material, no miner cooperation, and no chain state. The attacker-controlled entry path is `send_transaction` RPC → tx-pool admission → `NonContextualTransactionVerifier::verify()` → `ScriptHashTypeVerifier::verify()`. The omission is reachable on every node that processes externally submitted transactions.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to apply the same `ENABLED_SCRIPT_HASH_TYPE` check to the type script when it is present:

```rust
for output in self.transaction.outputs() {
    // existing lock-script check …

    // add: type-script check
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
```

This mirrors the fix applied in the ERC777 report (calling `send` for ERC777 tokens): handle the second "interface" (type script) with the same rigour as the first (lock script).

---

### Proof of Concept

1. Construct a `Transaction` with one output whose `lock` script uses a standard, permitted `hash_type` (e.g., `0x01` = `Type`) and whose `type_` script uses a `hash_type` byte that is a valid `ScriptHashType` variant but absent from `ENABLED_SCRIPT_HASH_TYPE` (e.g., a value reserved for a future hard fork).
2. Submit via `send_transaction` RPC.
3. `NonContextualTransactionVerifier::verify()` calls `ScriptHashTypeVerifier::verify()`, which iterates outputs, reads `output.lock().hash_type()` (valid → passes), and never reads `output.type_().hash_type()` → returns `Ok(())`.
4. The transaction proceeds to contextual verification. Nodes whose script layer does not recognise the hash type reject it there; nodes whose script layer does recognise it may accept it — producing divergent chain state. [1](#0-0) [5](#0-4)

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

**File:** verification/src/transaction_verifier.rs (L159-172)
```rust
    /// Perform context-dependent verification, return a `Result` to `CacheEntry`
    ///
    /// skip script verify will result in the return value cycle always is zero
    pub fn verify(&self, max_cycles: Cycle, skip_script_verify: bool) -> Result<Completed, Error> {
        self.time_relative.verify()?;
        self.capacity.verify()?;
        let cycles = if skip_script_verify {
            0
        } else {
            self.script.verify(max_cycles)?
        };
        let fee = self.fee_calculator.transaction_fee()?;
        Ok(Completed { cycles, fee })
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
