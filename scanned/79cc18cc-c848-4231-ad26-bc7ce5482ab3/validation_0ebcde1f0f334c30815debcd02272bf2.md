Based on my direct code inspection, the claim is factually accurate. Let me verify the key assertions:

**`ScriptHashTypeVerifier::verify`** only checks `output.lock().hash_type()`, never `output.type_()`: [1](#0-0) 

**`ENABLED_SCRIPT_HASH_TYPE`** contains `{0, 1, 2, 4}` — `Data3 = 6` is absent: [2](#0-1) 

**`check_data`** validates both lock and type scripts for structural validity only (not enablement): [3](#0-2) 

**`select_version`** is where non-enabled hash types are actually rejected — in contextual verification: [4](#0-3) 

The code gap is real and confirmed. The exploit path is reachable by an unprivileged user. The impact is concrete: unnecessary contextual verification work (cell-dep resolution, script group building) is performed before rejection. However, the claim's own language acknowledges "The impact is bounded but real," which is more consistent with a performance/efficiency issue than with "easily" causing network congestion. The attacker must pay fees proportional to transaction size (~1500 outputs is a large, costly transaction), limiting the cost asymmetry.

---

Audit Report

## Title
`ScriptHashTypeVerifier` Omits Type Script `hash_type` Enablement Check, Allowing Avoidable Contextual Verification Work — (`verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify` iterates all transaction outputs but only validates the lock script's `hash_type` against `ENABLED_SCRIPT_HASH_TYPE`. The type script's `hash_type` is never checked at this non-contextual stage. A transaction with a structurally valid but non-enabled type script `hash_type` (e.g., `Data3 = 6`) passes all non-contextual checks and proceeds into contextual verification — incurring cell-dep resolution and script-group-building overhead — before being rejected at `select_version`.

## Finding Description
`ScriptHashTypeVerifier::verify` loops over outputs and checks only `output.lock().hash_type()` against `ENABLED_SCRIPT_HASH_TYPE = {0, 1, 2, 4}`. The `output.type_()` field is never inspected here. The earlier `check_data` guard validates both lock and type scripts, but only for structural validity via `ScriptHashType::verify_value` — `Data3 = 6` is an even number and a valid molecule-encoded byte, so it passes `verify_value` and `check_data`. It is only rejected later in `select_version` during contextual script execution. The call chain is:

```
submit_transaction
  → NonContextualTransactionVerifier::verify
      → ScriptHashTypeVerifier::verify   ← passes (only checks lock)
  → ContextualTransactionVerifier::verify
      → select_version(type_script)      ← fails here (Data3 not enabled)
```

All contextual work (cell-dep resolution, script group construction) between these two points is performed and then discarded.

## Impact Explanation
This maps to **Low (501–2000 points): Any other important performance improvements for CKB**. The overhead per transaction is bounded and the attacker must pay fees proportional to transaction size, limiting the cost asymmetry. The claim's own language acknowledges the impact is "bounded but real" — this does not meet the threshold for "easily cause CKB network congestion with few costs" (High). The concrete impact is avoidable per-transaction verification overhead proportional to output count, which is a meaningful but bounded performance issue.

## Likelihood Explanation
The exploit is fully unprivileged and reachable via standard transaction submission RPC or P2P relay. `Data3 = 6` passes deserialization, `check_data`, and `ScriptHashTypeVerifier`. No special access, keys, or hashpower is required. The attacker cost is a transaction fee, which scales with transaction size (a 1500-output transaction is not cheap), limiting repeatability at scale.

## Recommendation
Extend `ScriptHashTypeVerifier::verify` to also validate `output.type_()` when present, mirroring the existing lock check:

```rust
if let Some(type_script) = output.type_().to_opt() {
    if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_script.hash_type()) {
        let val: u8 = hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
        }
    } else {
        return Err((TransactionError::InvalidScriptHashType {
            hash_type: type_script.hash_type(),
        }).into());
    }
}
```

This keeps rejection cheap and consistent with the existing lock-script check inside `ScriptHashTypeVerifier::verify`.

## Proof of Concept
```rust
let type_script = Script::default()
    .as_builder()
    .hash_type(6u8.into())  // Data3 = 6, not in ENABLED_SCRIPT_HASH_TYPE
    .build();
let output = CellOutput::new_builder()
    .lock(Script::default())           // hash_type = Data(0), valid
    .type_(Some(type_script).pack())
    .build();
let tx = TransactionBuilder::default()
    .outputs(vec![output; 100])
    .build();

let verifier = ScriptHashTypeVerifier::new(&tx);
assert!(verifier.verify().is_ok());   // passes — type hash_type never checked
// Contextual verification then rejects at select_version after performing
// cell-dep resolution and script-group construction for all 100 outputs.
```

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

**File:** util/constant/src/consensus.rs (L7-11)
```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
```

**File:** util/gen-types/src/extension/check_data.rs (L24-27)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
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
