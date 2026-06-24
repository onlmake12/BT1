The code confirms the claim. `ScriptHashTypeVerifier::verify()` at lines 796-814 iterates over outputs and checks only `output.lock().hash_type()`, never touching `output.type_()`. The `ENABLED_SCRIPT_HASH_TYPE` set is `{0, 1, 2, 4}`, and `select_version` in `script/src/types.rs` does catch invalid hash types — but only during the more expensive contextual verification path. No Security.md or Researcher.md exclusions exist in the repo.

Audit Report

## Title
`ScriptHashTypeVerifier` Omits Type Script Hash Type Validation, Allowing Bypass of Non-Contextual Tx-Pool Filter — (`verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` checks only the lock script's hash type for each transaction output, never inspecting the optional type script's hash type. A transaction with a valid lock script but an unpermitted type script hash type (e.g., `Data3`, byte value `6`) passes `NonContextualTransactionVerifier` and enters the tx-pool admission pipeline, triggering full contextual verification before being rejected at `select_version`. This undermines the two-stage cheap/expensive admission design and enables a low-cost DoS against the tx-pool's contextual verification path.

## Finding Description
`ENABLED_SCRIPT_HASH_TYPE` is defined as `{0, 1, 2, 4}` in `util/constant/src/consensus.rs` lines 7–11. [1](#0-0) 

`ScriptHashTypeVerifier::verify()` at `verification/src/transaction_verifier.rs` lines 796–814 iterates over outputs and validates only `output.lock().hash_type()`. The `output.type_()` field is never read. [2](#0-1) 

`ScriptHashTypeVerifier` is a sub-verifier of `NonContextualTransactionVerifier`, which is the sole non-contextual gate called by `tx-pool/src/util.rs`'s `non_contextual_verify` at lines 56–83. [3](#0-2) 

The downstream contextual path calls `select_version` in `script/src/types.rs` lines 900–936, which does reject unpermitted hash types via the catch-all arm at lines 930–935 — but only after input resolution and script verifier setup have already been performed. [4](#0-3) 

The test suite at `verification/src/tests/transaction_verifier.rs` lines 100–122 (`test_not_enabled_hash_type_output_lock`) confirms the lock-only scope and the absence of any analogous test for type scripts. [5](#0-4) 

Exploit path:
1. Attacker holds any live UTXO.
2. Constructs a transaction with a valid lock script (e.g., `Type` hash type) and a type script with `hash_type = 6` (`Data3`).
3. Submits via `send_transaction` RPC.
4. `non_contextual_verify` returns `Ok` — lock hash type is valid; type hash type is never checked.
5. Contextual verification is triggered: inputs are resolved from the database, `ScriptVerifier` is initialized, `select_version` is called and returns `InvalidScriptHashType`.
6. Transaction is rejected, but the cost of contextual verification (input resolution, verifier setup) has already been paid.
7. Repeat with distinct outputs/amounts using the same UTXO to generate an unbounded stream of such transactions.

## Impact Explanation
This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** The non-contextual check is the intended cheap DoS barrier before the expensive contextual check. Its gap for type scripts allows any unprivileged RPC caller to continuously force contextual verification cycles (including cell resolution from the database and script verifier initialization) at the cost of a single live UTXO. This can saturate the tx-pool's verification worker threads and degrade node performance.

## Likelihood Explanation
The attacker requires only one live UTXO and access to the public `send_transaction` RPC endpoint. No special privileges, leaked keys, or victim mistakes are needed. The attack is repeatable without limit — each submission can vary outputs or amounts to produce a distinct transaction hash, preventing deduplication. The gap is directly reachable by any external user.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to also validate the hash type of each output's optional type script, mirroring the existing lock-script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock check
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err(TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            }.into());
        }
        // add type script check
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
    Ok(())
}
```

Add a corresponding unit test analogous to `test_not_enabled_hash_type_output_lock` that sets an invalid hash type on the type script and asserts rejection.

## Proof of Concept
1. Obtain any live UTXO on-chain.
2. Construct a transaction spending that UTXO with one output: lock script with `hash_type = 1` (`Type`), type script with `hash_type = 6` (`Data3`).
3. Submit via `send_transaction` RPC.
4. Observe: `non_contextual_verify` returns `Ok` (lock hash type is valid; type hash type is never checked by `ScriptHashTypeVerifier`).
5. Observe: contextual verification fails at `select_version` with `InvalidScriptHashType`.
6. Repeat with varied output values to generate distinct transaction hashes — each forces a full contextual verification cycle.

A minimal unit test to confirm the gap:
```rust
#[test]
pub fn test_not_enabled_hash_type_output_type() {
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .lock(Script::default()) // valid lock
                .type_(Some(
                    Script::default()
                        .as_builder()
                        .hash_type(ScriptHashType::Data3)
                        .build(),
                ).pack())
                .build(),
        )
        .build();
    let verifier = ScriptHashTypeVerifier::new(&transaction);
    // Currently returns Ok(()), demonstrating the gap
    assert!(verifier.verify().is_err());
}
```

### Citations

**File:** util/constant/src/consensus.rs (L7-11)
```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
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

**File:** tx-pool/src/util.rs (L56-63)
```rust
pub(crate) fn non_contextual_verify(
    consensus: &Consensus,
    tx: &TransactionView,
) -> Result<(), Reject> {
    NonContextualTransactionVerifier::new(tx, consensus)
        .verify()
        .map_err(Reject::Verification)?;

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

**File:** verification/src/tests/transaction_verifier.rs (L100-122)
```rust
#[test]
pub fn test_not_enabled_hash_type_output_lock() {
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .lock(
                    Script::default()
                        .as_builder()
                        .hash_type(ScriptHashType::Data3)
                        .build(),
                )
                .build(),
        )
        .build();
    let verifier = ScriptHashTypeVerifier::new(&transaction);

    assert_error_eq!(
        verifier.verify().unwrap_err(),
        TransactionError::ScriptHashTypeNotPermitted {
            hash_type: ScriptHashType::Data3.into(),
        },
    );
}
```
