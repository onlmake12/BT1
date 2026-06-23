### Title
Incomplete `ScriptHashType` Enforcement: Type Script `hash_type` Not Validated Against Consensus-Permitted Set — (`verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and checks that each output's **lock** script `hash_type` is within the consensus-permitted set (`ENABLED_SCRIPT_HASH_TYPE`), but it never checks the **type** script's `hash_type`. Any transaction sender can submit an output whose type script carries a `hash_type` value that is structurally valid (passes the lower-level `check_data` enum-range guard) yet is explicitly prohibited by the current consensus rules, bypassing the restriction entirely.

---

### Finding Description

`ScriptHashTypeVerifier::verify()` is the consensus-layer gate that enforces which `ScriptHashType` values are currently permitted. Its loop body reads:

```rust
for output in self.transaction.outputs() {
    if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
        let val: u8 = hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(…ScriptHashTypeNotPermitted…);
        }
    } else {
        return Err(…InvalidScriptHashType…);
    }
}
```

Only `output.lock().hash_type()` is ever read. `output.type_()` — the optional type script — is never inspected. The structural validity guard (`check_data`) that runs earlier only rejects enum values that exceed the maximum defined byte (e.g., `0x03` in the test suite), not values that are structurally valid but consensus-disabled (e.g., a future `Data2 = 0x04`). Therefore a type script carrying a consensus-restricted `hash_type` passes both `check_data` and `ScriptHashTypeVerifier`.

The pattern is directly analogous to the external report: the code checks one field of the structure (`lock.hash_type`) but omits the parallel check on the sibling field (`type_.hash_type`), leaving the sibling field completely unguarded at the consensus layer.

**Entry path:** Any unprivileged transaction sender submits a transaction whose output contains a type script with a `hash_type` value that is in the valid enum range but absent from `ENABLED_SCRIPT_HASH_TYPE`. The transaction passes `check_data`, passes `ScriptHashTypeVerifier`, enters the tx-pool, and can be included in a block.

---

### Impact Explanation

- A transaction with a type script using a consensus-restricted `hash_type` (e.g., a value gated behind a future hardfork activation) is accepted and committed to the chain before the hardfork activates.
- This breaks the hardfork activation invariant: the protocol guarantees that restricted script semantics cannot appear on-chain until the governing consensus rule is active. Violating this can cause consensus splits between nodes that have and have not upgraded, or cause the VM to execute the type script under undefined/unintended semantics.
- Because the type script governs cell lifecycle (issuance, transfer, destruction rules for tokens, NFTs, DAO cells, etc.), accepting an output with an unauthorized type script `hash_type` can permanently commit cells to the chain whose type-script semantics are undefined on current nodes, leading to irreversible state corruption or a chain split.

---

### Likelihood Explanation

The entry path requires only the ability to submit a transaction — available to any RPC caller or P2P peer. No privileged key, operator access, or majority hashpower is needed. The only precondition is knowledge of a `hash_type` value that is structurally valid but not yet in `ENABLED_SCRIPT_HASH_TYPE`, which is derivable from the public source code.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's `hash_type` when the type script is present:

```rust
for output in self.transaction.outputs() {
    // existing lock check …

    // add: type script check
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

---

### Proof of Concept

1. Identify a `hash_type` byte value that is within the valid enum range (passes `check_data`) but is absent from `ENABLED_SCRIPT_HASH_TYPE` (e.g., a value reserved for a future hardfork).
2. Construct a transaction output with a type script whose `hash_type` is set to that value; set the lock script to any currently-valid `hash_type`.
3. Submit the transaction via `send_transaction` RPC.
4. Observe: `ScriptHashTypeVerifier` does not reject the transaction (it only inspects `output.lock().hash_type()`); the transaction enters the pool and can be mined into a block.
5. The committed cell carries a type script with a consensus-prohibited `hash_type`, violating the hardfork activation invariant. [1](#0-0)

### Citations

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
