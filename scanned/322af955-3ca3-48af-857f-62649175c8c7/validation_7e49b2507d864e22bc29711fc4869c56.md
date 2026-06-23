### Title
`ScriptHashTypeVerifier` Checks Only Lock Script Hash Type, Skips Type Script — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and enforces that the lock script's `hash_type` is within `ENABLED_SCRIPT_HASH_TYPE`, but performs **no equivalent check on the type script's `hash_type`**. A transaction output carrying a type script with a hash type value outside the consensus-permitted set passes non-contextual verification and is admitted to the tx pool.

---

### Finding Description

In `verification/src/transaction_verifier.rs`, the verifier is documented as:

> "Verify that the ScriptHashType of transaction outputs is within the range permitted by the current consensus rules."

The implementation, however, only inspects `output.lock()`:

```rust
// verification/src/transaction_verifier.rs  L796-L814
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) =
            TryInto::<ScriptHashType>::try_into(output.lock().hash_type())  // ← lock only
        {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(...ScriptHashTypeNotPermitted...);
            }
        } else {
            return Err(...InvalidScriptHashType...);
        }
    }
    Ok(())
}
```

`output.type_()` (the optional type script) is never read. The consensus-permitted set is:

```rust
// util/constant/src/consensus.rs  L7-L12
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // Data
    1u8, // Type
    2u8, // Data1
    4u8, // Data2
};
```

Value `3` is absent — it is reserved/not yet enabled. A type script carrying `hash_type = 3` (or any future value added to the enum before being added to `ENABLED_SCRIPT_HASH_TYPE`) is invisible to this verifier.

By contrast, the structural `check_data` path in `util/gen-types/src/extension/check_data.rs` correctly validates **both** scripts:

```rust
// util/gen-types/src/extension/check_data.rs  L24-L28
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()  // both
    }
}
```

`check_data` is invoked during P2P message parsing. It only checks whether the value is a valid enum discriminant — it does **not** enforce the consensus-level `ENABLED_SCRIPT_HASH_TYPE` restriction. The `ScriptHashTypeVerifier` is the sole place that enforces the consensus restriction, and it misses the type script entirely.

---

### Impact Explanation

A transaction output with a type script whose `hash_type` is outside `ENABLED_SCRIPT_HASH_TYPE` (e.g., value `3`) will:

1. Pass `check_data` (value `3` may be a valid enum discriminant).
2. Pass `ScriptHashTypeVerifier` (type script is never examined).
3. Be admitted into the tx pool via `NonContextualTransactionVerifier`.
4. Trigger expensive contextual verification (CKB-VM script execution) before being rejected.

This creates a **tx-pool admission bypass**: the non-contextual gate that is supposed to cheaply reject consensus-invalid transactions is incomplete. An attacker with access to live cells can flood the tx pool with such transactions, forcing each node to spend CKB-VM cycles on contextual verification before eviction. Because `max_tx_verify_cycles` bounds each run, the cost per transaction to the attacker is low (one live cell input), while the cost to the node is a full script-execution attempt.

---

### Likelihood Explanation

The entry path is fully unprivileged:

- **RPC path**: any caller of `send_transaction` can submit a crafted transaction. The RPC layer calls `non_contextual_verify` → `NonContextualTransactionVerifier` → `ScriptHashTypeVerifier`, which passes.
- **P2P relay path**: a peer can relay such a transaction; `check_data` only validates enum-discriminant legality, not consensus-level enablement.

No special role, key, or majority hash power is required. The attacker only needs one or more live cells to construct valid inputs.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's `hash_type` when the type script is present, mirroring the pattern already used in `check_data`:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock-script check
        match TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            Ok(ht) => {
                let val: u8 = ht.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
                }
            }
            Err(_) => return Err(TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            }.into()),
        }
        // NEW: type-script check (optional field)
        if let Some(type_script) = output.type_().to_opt() {
            match TryInto::<ScriptHashType>::try_into(type_script.hash_type()) {
                Ok(ht) => {
                    let val: u8 = ht.into();
                    if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                        return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
                    }
                }
                Err(_) => return Err(TransactionError::InvalidScriptHashType {
                    hash_type: type_script.hash_type(),
                }.into()),
            }
        }
    }
    Ok(())
}
```

---

### Proof of Concept

1. Construct a `CellOutput` whose `lock` script uses `hash_type = 0` (Data, valid) and whose `type_` script uses `hash_type = 3` (not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Wrap it in a transaction with a live-cell input.
3. Submit via `send_transaction` RPC.
4. Observe: `NonContextualTransactionVerifier` (including `ScriptHashTypeVerifier`) returns `Ok(())`.
5. The transaction enters the pending pool and triggers a full CKB-VM contextual verification run before being evicted.

**Root cause lines:** [1](#0-0) 

**Consensus-permitted set (value `3` absent):** [2](#0-1) 

**Correct symmetric check in `check_data` (both lock and type):** [3](#0-2) 

**`ScriptHashTypeVerifier` is part of `NonContextualTransactionVerifier`:** [4](#0-3)

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

**File:** util/gen-types/src/extension/check_data.rs (L24-28)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
```
