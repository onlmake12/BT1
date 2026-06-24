Audit Report

## Title
`ScriptHashTypeVerifier` Omits Type-Script Hash-Type Check on Transaction Outputs — (`File: verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over every transaction output but only validates the `hash_type` byte of the **lock script**. The **type script** of each output is never inspected against `ENABLED_SCRIPT_HASH_TYPE`. A transaction whose output carries a type script with `hash_type = 0x06` (`Data3`) passes all non-contextual checks, is admitted to the tx pool, and is only rejected later during contextual script verification — after pool resources have already been consumed.

## Finding Description

`ScriptHashTypeVerifier::verify()` at `verification/src/transaction_verifier.rs` lines 796–814 loops over outputs and calls `output.lock().hash_type()` exclusively:

```rust
for output in self.transaction.outputs() {
    if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
        let val: u8 = hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) { … }
    } else { … }
}
```

There is no branch for `output.type_().to_opt()`. The `ENABLED_SCRIPT_HASH_TYPE` set in `util/constant/src/consensus.rs` contains only `{0, 1, 2, 4}`. The `ScriptHashType` enum (generated via `seq!` in `util/gen-types/src/core.rs` lines 9–32) includes `Data3 = 6`, `Data4 = 8`, … `Data127 = 254`. The low-level structural validator `ScriptHashType::verify_value` (`util/gen-types/src/core.rs` line 39) accepts any even byte or `1`, so `0x06` passes `check_data`. The transaction therefore clears `NonContextualTransactionVerifier` (called from `tx-pool/src/util.rs` line 60 via `non_contextual_verify`). Only when `ContextualTransactionVerifier` later calls `select_version()` (`script/src/types.rs` lines 930–935) does the catch-all arm fire and return `InvalidScriptHashType`, at which point the transaction is evicted. The comment on `NonContextualTransactionVerifier` itself (`verification/src/transaction_verifier.rs` line 70) reads "Check whether output **lock** hash type within enabled range," confirming the type-script path was never added.

## Impact Explanation

Matches **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker submits transactions that pass non-contextual checks but fail contextual verification. Because rejected transactions are never committed to a block, no fee is ever deducted. The attacker can repeat this indefinitely at zero on-chain cost. Each such transaction forces every receiving node to run `ContextualTransactionVerifier` (input resolution, script group construction, `select_version` dispatch) before eviction, wasting CPU and tx-pool processing bandwidth. Legitimate transactions queued behind the spam experience increased latency.

## Likelihood Explanation

No privilege is required. Any RPC caller or P2P peer can submit a raw transaction. Crafting the payload requires only setting the `hash_type` byte of a `Script` struct to `0x06`; no key material, miner cooperation, or Sybil capability is needed. `verify_value(6)` returns `true` (6 is even), so the structural check passes. The attack is trivially scriptable and fully repeatable.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to validate the type script of each output when one is present, reusing the same `ENABLED_SCRIPT_HASH_TYPE` logic already applied to lock scripts:

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
```

where `check_hash_type` encapsulates the existing `TryInto::<ScriptHashType>` + `ENABLED_SCRIPT_HASH_TYPE.contains` logic. Add a companion test mirroring `test_not_enabled_hash_type_output_lock` but placing `Data3` on the type script instead of the lock script.

## Proof of Concept

1. Build a transaction with one output whose **lock** script uses `ScriptHashType::Data` (permitted) and whose **type** script has `hash_type` byte set to `0x06` (`Data3`).
2. Submit via `send_transaction` RPC.
3. `NonContextualTransactionVerifier::verify()` returns `Ok(())` — `ScriptHashTypeVerifier` never inspects the type script (`verification/src/transaction_verifier.rs` lines 796–814).
4. The transaction enters the tx pool admission pipeline (`tx-pool/src/util.rs` line 60).
5. `ContextualTransactionVerifier` calls `select_version()` on the type script; the catch-all arm at `script/src/types.rs` lines 930–935 returns `Err(ScriptError::InvalidScriptHashType(…))` and the transaction is evicted.
6. Repeat from step 1 at zero fee cost.

The existing test `test_not_enabled_hash_type_output_lock` (`verification/src/tests/transaction_verifier.rs` lines 100–122) covers only the lock-script path; no counterpart exists for the type-script path, confirming the gap is untested.