### Title
`ScriptHashTypeVerifier` Silently Omits Output Type Script Hash-Type Enforcement, Creating a Perceived Safeguard That Does Not Exist — (`verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` is the sole non-contextual gate that enforces the `ENABLED_SCRIPT_HASH_TYPE` allowlist (`{Data=0, Type=1, Data1=2, Data2=4}`). However, the implementation iterates over transaction outputs and inspects **only the lock script's `hash_type`** field; the type script (`output.type_()`) of every output is never examined. The verifier's name and its position inside `NonContextualTransactionVerifier` create the perception that all script hash types in a transaction are validated before the transaction enters the tx pool or a block. That safeguard does not exist for type scripts. A transaction carrying an output whose type script uses a non-permitted hash type (e.g., `Data3 = 6`) passes the non-contextual gate silently and is only rejected — if at all — during the heavier contextual script-execution phase.

---

### Finding Description

**Root cause — `ScriptHashTypeVerifier::verify()` checks only lock scripts:**

```rust
// verification/src/transaction_verifier.rs  lines 796-814
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) =
            TryInto::<ScriptHashType>::try_into(output.lock().hash_type())  // ← lock only
        {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(
                    TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into(),
                );
            }
        } else {
            return Err((TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            }).into());
        }
    }
    Ok(())
}
```

`output.type_()` is never read. The grep over the entire file confirms zero occurrences of `output.type_()` inside `transaction_verifier.rs`.

**The perceived safeguard:**

`NonContextualTransactionVerifier` documents itself as performing "Check whether output lock hash type within enabled range" and embeds `ScriptHashTypeVerifier` as its enforcement mechanism. Callers — including the tx-pool admission path — invoke `non_contextual_verify()` → `NonContextualTransactionVerifier::verify()` → `ScriptHashTypeVerifier::verify()` and reasonably expect that any script with a disallowed hash type is rejected here. That expectation holds for lock scripts but is silently false for type scripts.

**Allowed set:**

```rust
// util/constant/src/consensus.rs  lines 7-12
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
};
```

`Data3 = 6`, `Data4 = 8`, … are valid bit-pattern values (even, so `verify_value()` passes) but are not in the enabled set. A type script carrying any of these values bypasses `ScriptHashTypeVerifier` entirely.

**Tx-pool admission path:**

```
RPC send_transaction / relay
  └─ non_contextual_verify()          [tx-pool/src/util.rs:56-82]
       └─ NonContextualTransactionVerifier::verify()
            └─ ScriptHashTypeVerifier::verify()   ← passes for bad type-script hash_type
  └─ verify_rtx()  (async, contextual)
       └─ ContextualTransactionVerifier::verify()
            └─ ScriptVerifier → select_version()  ← rejects here, but only if DISABLE_SCRIPT is not set
```

**Script-execution catch — and when it is absent:**

`select_version()` in `script/src/types.rs:930-935` returns `ScriptError::InvalidScriptHashType` for any `ScriptHashType` variant not explicitly matched (i.e., `Data3` and above). Under normal operation this is the backstop. However:

- When `Switch::DISABLE_SCRIPT` is active (used in integration tests and reachable via the `assume_valid_target` sync optimisation), script execution is skipped entirely. The non-contextual check is the **only** gate, and it does not cover type scripts.
- Even in normal operation, the transaction enters the tx pool and consumes verification resources before being evicted, opening a low-cost resource-exhaustion vector for any RPC/relay caller.

---

### Impact Explanation

1. **Tx-pool resource exhaustion (always reachable):** Any unprivileged RPC caller or relay peer can submit transactions whose outputs carry type scripts with `Data3`/`Data4`/… hash types. Each such transaction passes `non_contextual_verify()`, enters the pool, and forces a full contextual script-execution attempt before being evicted. Because the non-contextual gate is the intended cheap filter, this bypasses the intended cost model for pool admission.

2. **Silent block acceptance under `DISABLE_SCRIPT` / `assume_valid_target`:** When a node processes blocks with script verification disabled, the only hash-type enforcement is `ScriptHashTypeVerifier`. A block containing a transaction with a non-permitted type-script hash type is accepted by that node but rejected by fully-verifying peers, producing a local chain state inconsistent with consensus. A malicious block relayer can exploit this to split a syncing node from the honest chain.

---

### Likelihood Explanation

The attacker entry path requires no privilege: any `send_transaction` RPC caller or P2P relay peer can craft the transaction. Constructing an output with `type_script.hash_type = Data3 (6)` is a single-field change to an otherwise valid transaction. The non-contextual check is exercised on every submitted transaction, so the bypass is triggered deterministically on every such submission. The `assume_valid_target` scenario requires a node operator to have enabled that option, but the tx-pool resource-exhaustion path is unconditional.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type of every output:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock-script check
        check_hash_type(output.lock().hash_type())?;

        // add: type-script check
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}

fn check_hash_type(raw: packed::Byte) -> Result<(), Error> {
    match TryInto::<ScriptHashType>::try_into(raw) {
        Ok(ht) => {
            let val: u8 = ht.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
            Ok(())
        }
        Err(_) => Err((TransactionError::InvalidScriptHashType { hash_type: raw }).into()),
    }
}
```

Update the `NonContextualTransactionVerifier` doc comment from "Check whether output **lock** hash type within enabled range" to "Check whether output lock and type script hash types are within the enabled range" to accurately reflect the intended invariant.

---

### Proof of Concept

1. Build a transaction with one output whose **type script** has `hash_type = 6` (`Data3`, a valid bit-pattern but not in `ENABLED_SCRIPT_HASH_TYPE`) and a normal lock script (`Data = 0`).
2. Submit via `send_transaction` RPC.
3. `non_contextual_verify()` calls `ScriptHashTypeVerifier::verify()`, which iterates outputs and checks only `output.lock().hash_type() = 0` → passes.
4. The transaction is admitted to the tx pool.
5. `verify_rtx()` eventually calls `select_version()` on the type script; `Data3` hits the catch-all arm and returns `ScriptError::InvalidScriptHashType` → transaction is evicted.
6. Repeat at high rate: each submission forces a full contextual verification cycle that the non-contextual gate was designed to prevent.
7. On a node running with `Switch::DISABLE_SCRIPT` (or `assume_valid_target` covering the target height), step 5 is skipped and the transaction is committed to the block without error, diverging from fully-verifying peers.

**Relevant code locations:**
- `verification/src/transaction_verifier.rs` lines 785–815 (`ScriptHashTypeVerifier`)
- `verification/src/transaction_verifier.rs` lines 61–103 (`NonContextualTransactionVerifier`)
- `tx-pool/src/util.rs` lines 56–82 (`non_contextual_verify`)
- `util/constant/src/consensus.rs` lines 7–12 (`ENABLED_SCRIPT_HASH_TYPE`)
- `script/src/types.rs` lines 900–937 (`select_version` — the only backstop, absent when script execution is disabled)