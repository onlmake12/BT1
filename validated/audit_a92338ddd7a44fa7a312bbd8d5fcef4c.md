All cited code references are confirmed in the repository. Here is the validation:

---

Audit Report

## Title
`ScriptHashTypeVerifier` Skips Type Script `hash_type` Validation, Enabling Peer-Ban Bypass and Verify-Queue Resource Exhaustion — (File: `verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates only the lock script `hash_type` against `ENABLED_SCRIPT_HASH_TYPE`, leaving the optional type script `hash_type` entirely unchecked. An attacker can craft transactions with a structurally valid but consensus-disabled type script `hash_type` (e.g., a defined-but-not-yet-activated variant like `Data3 = 6`), pass non-contextual verification, enter the verify queue, and be rejected only by the contextual script verifier — which emits a `ScriptError`, not a `TransactionError`, so `is_malformed_tx()` returns `false` and the peer is never banned. Because the UTXO is not consumed on rejection, the attacker can reuse it to flood the verify queue indefinitely at near-zero cost.

## Finding Description

**Root cause.** `ScriptHashTypeVerifier::verify()` at lines 796–814 of `verification/src/transaction_verifier.rs` loops over outputs and calls only `output.lock().hash_type()`: [1](#0-0) 

`output.type_()` — the optional type script — is never inspected. `ENABLED_SCRIPT_HASH_TYPE` is `{0, 1, 2, 4}` (Data, Type, Data1, Data2): [2](#0-1) 

**Why `check_data` is insufficient.** `CellOutputReader::check_data()` calls `self.lock().check_data() && self.type_().check_data()`, and `ScriptOptReader::check_data()` calls `core::ScriptHashType::verify_value(i.hash_type().into())`: [3](#0-2) 

`verify_value` is a structural/enum-membership check — it accepts any defined `ScriptHashType` variant. A variant like `Data3 = 6` that is defined in the enum but not yet activated passes `verify_value` (it is a known variant), but is absent from `ENABLED_SCRIPT_HASH_TYPE`. The `select_version()` error message in `script/src/types.rs` explicitly confirms this class of values exists ("has not been activated"): [4](#0-3) 

**Exploit flow.**
1. Attacker holds one valid UTXO.
2. Attacker crafts a transaction: valid lock script (`hash_type = 1`), type script with `hash_type = 6` (defined but not activated), varying outputs for distinct tx hashes.
3. `check_data` passes — `verify_value(6)` accepts the defined variant.
4. `NonContextualTransactionVerifier::verify()` calls `script_hash_type.verify()`, which returns `Ok(())` because only the lock script is checked: [5](#0-4) 

5. `non_contextual_verify` in `tx-pool/src/process.rs` sees no error, so no ban is issued and the transaction is enqueued: [6](#0-5) 

6. The contextual script verifier calls `select_version()`, which returns `ScriptError::InvalidScriptHashType` for `hash_type = 6`.
7. `ScriptError` is not a `TransactionError`. `is_malformed_tx()` only covers `TransactionError` variants: [7](#0-6) 

No `ban_malformed` call is triggered. The UTXO is not consumed. The attacker repeats with a new output variation.

**Asymmetry.** A lock script with `hash_type = 6` triggers `TransactionError::ScriptHashTypeNotPermitted` → `is_malformed_tx() = true` → peer ban. An identical transaction with the same invalid `hash_type` on the type script does not.

## Impact Explanation

This matches the High impact class: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** An attacker with a single UTXO can generate an unbounded stream of distinct transactions (varying output values/counts) that each pass non-contextual verification, occupy verify-queue slots, and consume CPU in the contextual verifier before rejection — with no peer-ban penalty and no UTXO cost, since rejected transactions do not spend inputs. The verify queue has finite capacity; sustained flooding degrades or blocks legitimate transaction processing.

## Likelihood Explanation

Any unprivileged P2P peer or RPC caller can trigger this. No special keys, majority hashpower, or victim interaction is required. The attacker needs only one spendable cell output. The attack is repeatable indefinitely because the UTXO is never consumed on rejection and the peer is never banned. Crafting the malformed type script requires setting a single byte (`hash_type = 6`) in the transaction binary.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to check the type script `hash_type` for each output, mirroring the existing lock-script logic:

```rust
// After the lock-script check, inside the for loop:
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
        })
        .into());
    }
}
```

Also update the `NonContextualTransactionVerifier` doc comment at line 70 from "output lock hash type" to "output lock and type script hash type": [8](#0-7) 

## Proof of Concept

1. Obtain one spendable UTXO on a CKB test node.
2. Construct a raw transaction: valid lock script (`hash_type = 1`), type script with `hash_type = 6` (`Data3`, defined but not activated), any valid capacity output.
3. Relay the transaction to the node via P2P (`RelayTransaction` message). Confirm `check_data` passes (`verify_value(6)` accepts the defined variant).
4. Observe that `ScriptHashTypeVerifier::verify()` returns `Ok(())` — no non-contextual rejection.
5. Observe the transaction enters the verify queue.
6. Observe the transaction is rejected by `select_version()` with `ScriptError::InvalidScriptHashType`, not `TransactionError::InvalidScriptHashType`.
7. Confirm `is_malformed_tx()` returns `false` for this rejection and no `ban_malformed` is called.
8. Repeat steps 2–7 with a different output amount (new tx hash) using the same UTXO — confirm the UTXO is still unspent and the peer is still not banned.
9. Contrast: submit an identical transaction with `hash_type = 6` on the **lock** script instead — confirm immediate peer banning via `TransactionError::ScriptHashTypeNotPermitted`.

### Citations

**File:** verification/src/transaction_verifier.rs (L70-70)
```rust
/// - Check whether output lock hash type within enabled range
```

**File:** verification/src/transaction_verifier.rs (L94-102)
```rust
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

**File:** util/gen-types/src/extension/check_data.rs (L16-22)
```rust
impl<'r> packed::ScriptOptReader<'r> {
    fn check_data(&self) -> bool {
        self.to_opt()
            .map(|i| core::ScriptHashType::verify_value(i.hash_type().into()))
            .unwrap_or(true)
    }
}
```

**File:** script/src/types.rs (L930-935)
```rust
            hash_type => {
                return Err(ScriptError::InvalidScriptHashType(format!(
                    "The ScriptHashType/{:?} has not been activated, and is not permitted for use.",
                    hash_type
                )));
            }
```

**File:** tx-pool/src/process.rs (L318-333)
```rust
    pub(crate) async fn non_contextual_verify(
        &self,
        tx: &TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<(), Reject> {
        if let Err(reject) = non_contextual_verify(&self.consensus, tx) {
            if reject.is_malformed_tx()
                && let Some(remote) = remote
            {
                self.ban_malformed(remote.1, format!("reject {reject}"))
                    .await;
            }
            return Err(reject);
        }
        Ok(())
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
