Audit Report

## Title
Missing Type Script Hash Type Validation in `ScriptHashTypeVerifier` - (File: `verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` iterates over every transaction output but only validates `output.lock().hash_type()` against `ENABLED_SCRIPT_HASH_TYPE`, never checking `output.type_().hash_type()`. A transaction carrying a type script with a future/not-yet-activated `ScriptHashType` (e.g., `Data3` = 6) passes `NonContextualTransactionVerifier` silently, enters the verify queue, and is only rejected during expensive contextual verification — after database lookups, capacity checks, and script group construction — rather than at the cheap O(n) admission gate. Because the transaction is rejected without being mined, the attacker's UTXO is never consumed, enabling indefinite repetition at near-zero cost.

## Finding Description
`ScriptHashTypeVerifier::verify()` at `verification/src/transaction_verifier.rs` L796–814 loops over outputs and checks only the lock script:

```rust
for output in self.transaction.outputs() {
    if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
        ...
    }
    // output.type_() is never inspected
}
```

The upstream structural gate `check_data()` (`util/gen-types/src/extension/check_data.rs` L24–27) does check both lock and type scripts, but only via `ScriptHashType::verify_value()`, which accepts any even byte or `1` — so `Data3` (6), `Data4` (8), … `Data127` (254) all pass. The consensus-level allowlist `ENABLED_SCRIPT_HASH_TYPE` (`util/constant/src/consensus.rs` L7–11) restricts to `{0, 1, 2, 4}`, but `ScriptHashTypeVerifier` never applies it to the type script field.

A transaction with `type_script.hash_type = 6` therefore clears every non-contextual check. It is enqueued via `resumeble_process_tx` / `process_tx` (`tx-pool/src/process.rs` L335–352, L401–426), which call `non_contextual_verify` first and then `enqueue_verify_queue`. The contextual path (`verify_rtx` in `tx-pool/src/util.rs` L85–131) then resolves all inputs from the UTXO database, runs `TimeRelativeTransactionVerifier`, `CapacityVerifier`, and finally `ScriptVerifier`. Inside `ScriptVerifier`, `select_version()` (`script/src/types.rs` L900–936) hits the catch-all arm and returns `ScriptError::InvalidScriptHashType` — only at this point is the transaction rejected. No VM bytecode is executed, but the full resolution and pre-execution pipeline has already run.

Because the transaction is rejected (never mined), the input UTXO remains unspent. The attacker can immediately craft a new transaction with the same input and a different output (different hash, not in `recent_reject`), and the cycle repeats.

## Impact Explanation
This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

Each crafted transaction forces every receiving node to perform: UTXO database lookups for all inputs, time-relative and capacity verification, and script group construction — all work that should have been short-circuited at the non-contextual gate. Because the UTXO is never consumed, a single UTXO is sufficient to sustain the attack indefinitely. The verify queue can be kept saturated with invalid work, degrading throughput for legitimate transactions. The same path is exercised for P2P-relayed transactions, so the attack surface extends to every node that receives the relay.

## Likelihood Explanation
Any unprivileged actor with a single live UTXO can trigger this. Setting `hash_type` to `6` in the serialized type script is a one-byte change. No key material beyond ownership of one UTXO is required, no mining power is needed, and the attack is repeatable at negligible marginal cost because the UTXO is never spent. The existing test `test_not_enabled_hash_type_output_lock` (`verification/src/tests/transaction_verifier.rs` L100–122) confirms the lock-script path is correctly rejected; the absence of an equivalent test for the type script confirms the gap is untested and unguarded.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type for every output that carries one:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        check_hash_type(output.lock().hash_type())?;
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
                Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into())
            } else {
                Ok(())
            }
        }
        Err(_) => Err(TransactionError::InvalidScriptHashType { hash_type: raw }.into()),
    }
}
```

Add a companion test mirroring `test_not_enabled_hash_type_output_lock` but placing `ScriptHashType::Data3` in the type script field.

## Proof of Concept
1. Build a transaction whose output has a valid lock (`Data = 0`) and a type script with `hash_type = 6` (`Data3`):

```rust
let tx = TransactionBuilder::default()
    .output(
        CellOutput::new_builder()
            .lock(Script::default())
            .type_(Some(
                Script::default().as_builder()
                    .hash_type(ScriptHashType::Data3)
                    .build(),
            ).pack())
            .build(),
    )
    .output_data(Bytes::new().pack())
    .build();
```

2. `ScriptHashTypeVerifier::new(&tx).verify()` returns `Ok(())` — confirmed by the code at L796–814 which never touches `output.type_()`.

3. The same value in the lock script is correctly rejected, as proven by the existing test at `verification/src/tests/transaction_verifier.rs` L100–122.

4. Submit via `send_transaction` RPC or P2P relay. The transaction passes `non_contextual_verify`, enters the verify queue, triggers full contextual resolution, and is rejected only at `select_version()` in `script/src/types.rs` L930–935 with `ScriptError::InvalidScriptHashType`. The submitting UTXO is not consumed; the attacker repeats with a new output to generate a fresh transaction hash, sustaining the attack indefinitely.