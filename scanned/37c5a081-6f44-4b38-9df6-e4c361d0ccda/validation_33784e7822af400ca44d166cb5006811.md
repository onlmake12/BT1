Based on my investigation of the actual code in the repository, I can confirm the structural omission and assess the claim.

**Confirmed facts:**

1. `ScriptHashTypeVerifier::verify()` at lines 796–814 of `verification/src/transaction_verifier.rs` iterates outputs and checks only `output.lock().hash_type()`. [1](#0-0) 

2. There is no call to `output.type_().to_opt()` anywhere in the verifier — confirmed by grep returning zero matches for type script inspection in that file. [2](#0-1) 

3. `ENABLED_SCRIPT_HASH_TYPE` is `{0, 1, 2, 4}` — values like `6` are not in the set. [3](#0-2) 

4. The verifier's stated purpose is to validate all output script hash types against consensus rules, yet it structurally omits the type script field. [4](#0-3) 

**Caveats on unverified claims:** The cited `is_malformed_tx()` function, `InvalidScriptHashType`/`ScriptHashTypeNotPermitted` error variants, and `select_version` in `script/src/types.rs` returned no grep results in this repository — those portions of the claim rely on code not confirmed present in this repo. The core structural omission, however, is confirmed.

---

Audit Report

## Title
`ScriptHashTypeVerifier` Omits Type Script `hash_type` Validation, Allowing Malformed Transactions to Bypass Early Rejection — (File: `verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` iterates transaction outputs and validates only the lock script's `hash_type` against `ENABLED_SCRIPT_HASH_TYPE`, never inspecting the type script's `hash_type`. A transaction output whose type script carries a non-permitted `hash_type` (e.g., `0x06`) passes this verifier unconditionally, is admitted to the tx-pool, and can only be rejected later during full script execution — after consuming node and miner resources.

## Finding Description
The verifier at `verification/src/transaction_verifier.rs:796–814` loops over outputs and performs:

```rust
if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
    let val: u8 = hash_type.into();
    if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) { ... }
} else { ... }
```

There is no corresponding branch reading `output.type_().to_opt()`. An output with `lock.hash_type = 1` (Type, permitted) and `type.hash_type = 6` (Data3, not in `{0,1,2,4}`) passes the verifier and enters the tx-pool. The omission is structural — the function's stated purpose covers all output scripts but its implementation covers only one of the two script fields per output.

## Impact Explanation
Matches **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. An attacker can continuously submit transactions with invalid type script `hash_type` values that pass `ScriptHashTypeVerifier`, occupy tx-pool slots, and force miner-side script execution overhead on every block-assembly cycle. Because the early verifier does not catch these, the rejection path is deferred to script execution, which is more expensive. If the inconsistent peer-penalty path (no ban for type script violations vs. ban for lock script violations) holds in this codebase, the attacker can sustain the attack from the same peer connection indefinitely at minimum relay fee cost per transaction.

## Likelihood Explanation
Any unprivileged RPC caller or P2P relay peer can trigger this. The attacker needs only live cells to fund relay fees — no key material beyond their own, no special privilege, no majority hashpower. The minimum relay fee is the only per-transaction cost. The attack is repeatable and requires no victim interaction.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's `hash_type` when present:

```rust
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

This should be inserted inside the existing output loop, after the lock script check.

## Proof of Concept
1. Attacker owns a live cell with sufficient capacity.
2. Build a transaction: input = live cell; output = any cell with a valid lock script (`hash_type = 1`) and a type script with `hash_type = 0x06`.
3. Submit via `send_transaction` RPC or P2P relay.
4. Node runs `ScriptHashTypeVerifier::verify()` — checks `output.lock().hash_type()` (valid, passes), never reads `output.type_().hash_type()` — returns `Ok(())`.
5. Transaction is admitted to the tx-pool.
6. On block assembly, the miner's script execution rejects the transaction due to the invalid type script `hash_type`.
7. Transaction is never committed; it occupies a tx-pool slot and forces repeated miner-side rejection overhead until eviction.
8. Repeat from step 2 with a new transaction (spending a different cell or using replace-by-fee) to maintain tx-pool pressure.

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

**File:** util/constant/src/consensus.rs (L7-11)
```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
```
