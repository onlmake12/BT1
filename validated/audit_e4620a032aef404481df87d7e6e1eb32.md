### Title
`ScriptHashTypeVerifier` Validates Output Lock Script Hash Type But Not Output Type Script Hash Type - (File: `verification/src/transaction_verifier.rs`)

### Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and enforces that the **lock script's** `hash_type` is within the set of consensus-permitted values (`ENABLED_SCRIPT_HASH_TYPE`), but performs no equivalent check on the **type script's** `hash_type`. An unprivileged transaction sender can craft a transaction whose output cell carries a type script with a disallowed or not-yet-hardfork-enabled `hash_type`, bypassing this consensus gate entirely.

---

### Finding Description

The verifier's stated purpose (per its own doc-comment) is:

> "Verify that the ScriptHashType of transaction outputs is within the range permitted by the current consensus rules."

The implementation only fulfils half of that contract:

```rust
// verification/src/transaction_verifier.rs
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) =
            TryInto::<ScriptHashType>::try_into(output.lock().hash_type())  // ← lock only
        {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(…ScriptHashTypeNotPermitted…);
            }
        } else {
            return Err(…InvalidScriptHashType…);
        }
    }
    Ok(())
}
```

`output.type_()` is never inspected. A `CellOutput` carries both a mandatory `lock` script and an optional `type_` script; both fields contain an independent `hash_type` byte. The analogous check for the type script is simply absent. [1](#0-0) 

For comparison, the low-level P2P deserialization layer (`check_data`) does validate **both** lock and type hash types on received messages:

```rust
// util/gen-types/src/extension/check_data.rs
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()   // both sides
    }
}
``` [2](#0-1) 

That P2P-layer guard only rejects structurally invalid bytes (e.g., raw byte value `3` which is currently reserved); it does not enforce the consensus-level `ENABLED_SCRIPT_HASH_TYPE` allowlist. The consensus allowlist enforcement is exclusively the job of `ScriptHashTypeVerifier`, which skips type scripts.

---

### Impact Explanation

CKB introduces new `ScriptHashType` variants (e.g., `Data2`) via hardforks. `ENABLED_SCRIPT_HASH_TYPE` is the consensus gate that prevents pre-hardfork nodes from accepting transactions that use not-yet-activated hash types.

Because the gate is only applied to lock scripts, an attacker can submit a transaction whose output type script uses a hash type that is:

1. **Not yet hardfork-activated** – the output passes `ScriptHashTypeVerifier` (lock script is fine), proceeds to script execution, and if the VM/scheduler does not independently reject the unactivated hash type, the cell is committed to the UTXO set. Nodes at different software versions may disagree on validity → **consensus split / chain fork**.
2. **Structurally invalid** – if the byte value is unrecognised by the VM, the node may panic or produce an unhandled error path rather than a clean consensus rejection, depending on how the script scheduler handles the `TryInto<ScriptHashType>` failure for output type scripts. [3](#0-2) 

---

### Likelihood Explanation

Any RPC caller or P2P peer can submit a raw transaction. Constructing a `CellOutput` with an arbitrary `hash_type` byte in the type script requires only knowledge of the molecule serialisation format, which is fully public. No privileged role, key material, or majority hashpower is needed. The attack surface is the standard `send_transaction` RPC endpoint and the P2P transaction relay path.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also check the `hash_type` of the output's type script when one is present, mirroring the existing lock-script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock check …
        check_hash_type(output.lock().hash_type())?;

        // add: type script check
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}
```

This mirrors the symmetric validation already present in the P2P `check_data` layer and matches the pattern used in `Inch_Module` (validating both input and output tokens) that the reference report recommends.

---

### Proof of Concept

A transaction sender constructs a `CellOutput` where the lock script uses a valid, enabled hash type (e.g., `Type = 0x01`) and the type script uses a disallowed hash type byte (e.g., `0x04`, a future variant not yet in `ENABLED_SCRIPT_HASH_TYPE`):

```
CellOutput {
    lock: Script { hash_type: 0x01 /* Type – passes verifier */ },
    type: Script { hash_type: 0x04 /* future/disallowed – NOT checked */ },
}
```

`ScriptHashTypeVerifier::verify()` iterates the output, checks `output.lock().hash_type() == 0x01` → permitted → `Ok(())`. The type script's `hash_type: 0x04` is never read. The transaction proceeds to the script execution stage. Depending on whether the VM scheduler independently rejects the unactivated hash type, the transaction may be committed, creating a UTXO with a type script that violates the current consensus rules — an exact analog of the "non-approved output token stuck in the contract" scenario from the reference report. [4](#0-3) [2](#0-1)

### Citations

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

**File:** util/gen-types/src/extension/check_data.rs (L24-28)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
```
