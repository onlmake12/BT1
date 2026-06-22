### Title
`ScriptHashTypeVerifier` Omits Type Script `hash_type` Validation, Allowing Malformed Transactions to Bypass Early Rejection — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates only the **lock script's** `hash_type` against the consensus-permitted set (`ENABLED_SCRIPT_HASH_TYPE`). It never inspects the **type script's** `hash_type`. An unprivileged transaction sender can craft an output whose type script carries a non-permitted `hash_type` value (e.g., `Data3 = 6`, `Data4 = 8`, …) and have that transaction pass the verifier, be admitted to the tx-pool, and only be rejected later—during full script execution—at greater node cost and with a different error path.

---

### Finding Description

`ENABLED_SCRIPT_HASH_TYPE` defines the consensus-permitted set:

```
0u8  // Data
1u8  // Type
2u8  // Data1
4u8  // Data2
``` [1](#0-0) 

`ScriptHashTypeVerifier::verify()` loops over every output and checks only `output.lock().hash_type()`:

```rust
for output in self.transaction.outputs() {
    if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
        let val: u8 = hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) { … }
    } else { … }
}
``` [2](#0-1) 

There is no corresponding branch that calls `output.type_().to_opt()` and validates its `hash_type`. The verifier's stated purpose is:

> *"Verify that the ScriptHashType of transaction outputs is within the range permitted by the current consensus rules."* [3](#0-2) 

Yet it silently ignores the type script field entirely. A transaction output such as:

```
lock: { code_hash: <valid>, hash_type: Type (1) }   ← checked ✓
type: { code_hash: <any>,   hash_type: Data3 (6) }  ← never checked ✗
```

passes `ScriptHashTypeVerifier` without error.

The script execution engine does catch this later—`select_version` returns `InvalidScriptHashType` for any unrecognised `hash_type`: [4](#0-3) 

But that rejection happens only after the transaction has already been admitted to the tx-pool and a miner has attempted to include it in a block.

---

### Impact Explanation

1. **Tx-pool pollution.** A transaction with a type script carrying `hash_type = 6` (or any even value ≥ 6 not in the permitted set) passes `ScriptHashTypeVerifier` and is inserted into the tx-pool. It will never be successfully committed because script execution rejects it, yet it occupies a slot until evicted.

2. **Miner resource waste.** When the miner's block-assembly loop pulls the transaction and runs script verification, it pays the cost of resolving cell deps and entering the VM dispatch path before the `InvalidScxtHashType` error is returned. This is avoidable overhead that the early verifier was designed to eliminate.

3. **Inconsistent error classification.** `ScriptHashTypeNotPermitted` (raised by `ScriptHashTypeVerifier`) is classified as `is_malformed_tx() == true`, which triggers a ban/penalty path for the submitting peer. A type script with the same invalid `hash_type` escapes that classification entirely, receiving only a script-execution error with no peer penalty. [5](#0-4) 

---

### Likelihood Explanation

Any unprivileged RPC caller or P2P relay peer can submit a transaction. Constructing an output with a type script whose `hash_type` byte is set to `6` requires no special privilege, no key material, and no majority hashpower. The attacker must own live cells to fund the transaction fee, but the cost per malformed transaction is the minimum relay fee—a low barrier for sustained tx-pool pollution.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's `hash_type` when one is present:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock-script check
        let lock_hash_type = TryInto::<ScriptHashType>::try_into(output.lock().hash_type())
            .map_err(|_| TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            })?;
        let val: u8 = lock_hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
        }

        // NEW: type-script check
        if let Some(type_script) = output.type_().to_opt() {
            let type_hash_type = TryInto::<ScriptHashType>::try_into(type_script.hash_type())
                .map_err(|_| TransactionError::InvalidScriptHashType {
                    hash_type: type_script.hash_type(),
                })?;
            let val: u8 = type_hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(
                    TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into()
                );
            }
        }
    }
    Ok(())
}
```

---

### Proof of Concept

**Attacker-controlled entry path:** RPC `send_transaction` or P2P relay.

**Steps:**

1. Attacker owns a live cell with sufficient capacity.
2. Attacker builds a transaction:
   - Input: the live cell.
   - Output: any cell whose `type` script has `hash_type = 0x06` (Data3, not in `ENABLED_SCRIPT_HASH_TYPE`), lock script is valid.
3. Attacker submits via `send_transaction`.
4. Node runs `ScriptHashTypeVerifier::verify()` — iterates outputs, checks `output.lock().hash_type()` (valid), **never reads** `output.type_().hash_type()` — returns `Ok(())`.
5. Transaction is admitted to the tx-pool.
6. Miner pulls the transaction for block assembly; `select_version` hits the `hash_type => Err(InvalidScriptHashType)` arm and rejects it.
7. Transaction is never committed; it occupies a tx-pool slot and forces miner-side script-execution overhead on every block-assembly cycle until eviction.

The root cause is the structural omission at: [2](#0-1) 

which mirrors the original report's pattern: a validation function accepts a structured input (transaction output) and checks one field (lock `hash_type`) while leaving the analogous field (type `hash_type`) entirely unchecked.

### Citations

**File:** util/constant/src/consensus.rs (L7-12)
```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
};
```

**File:** verification/src/transaction_verifier.rs (L785-787)
```rust
// Verify that the ScriptHashType of transaction outputs
// is within the range permitted by the current consensus rules.
pub struct ScriptHashTypeVerifier<'a> {
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

**File:** script/src/types.rs (L930-936)
```rust
            hash_type => {
                return Err(ScriptError::InvalidScriptHashType(format!(
                    "The ScriptHashType/{:?} has not been activated, and is not permitted for use.",
                    hash_type
                )));
            }
        }
```

**File:** util/types/src/core/error.rs (L244-255)
```rust
    pub fn is_malformed_tx(&self) -> bool {
        match self {
            TransactionError::OutputsSumOverflow { .. }
            | TransactionError::DuplicateCellDeps { .. }
            | TransactionError::DuplicateHeaderDeps { .. }
            | TransactionError::Empty { .. }
            | TransactionError::InsufficientCellCapacity { .. }
            | TransactionError::InvalidSince { .. }
            | TransactionError::ExceededMaximumBlockBytes { .. }
            | TransactionError::InvalidScriptHashType { .. }
            | TransactionError::ScriptHashTypeNotPermitted { .. }
            | TransactionError::OutputsDataLengthMismatch { .. } => true,
```
