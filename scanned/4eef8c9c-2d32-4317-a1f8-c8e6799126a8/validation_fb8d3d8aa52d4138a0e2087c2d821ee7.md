Audit Report

## Title
`ScriptHashTypeVerifier` Skips Type-Script `hash_type` Validation, Causing Unnecessary Contextual Work — (`verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify` iterates over transaction outputs and validates only the **lock** script's `hash_type` against `ENABLED_SCRIPT_HASH_TYPE`, never inspecting the **type** script's `hash_type`. A transaction with a valid lock `hash_type` but an invalid type `hash_type` (e.g., `6` / `Data3`) passes `NonContextualTransactionVerifier` and proceeds into `ContextualTransactionVerifier`, where it runs `time_relative.verify()`, `capacity.verify()`, and enters `TransactionScriptsVerifier` before being rejected during script-group resolution. This is avoidable work that a live-cell holder can trigger repeatedly.

## Finding Description
`ENABLED_SCRIPT_HASH_TYPE` is defined as `{0, 1, 2, 4}` in `util/constant/src/consensus.rs` lines 7–11. `ScriptHashTypeVerifier::verify` (lines 796–814 of `verification/src/transaction_verifier.rs`) loops over outputs and calls only `output.lock().hash_type()`. There is no corresponding call to `output.type_().hash_type()`. The struct-level comment at line 70 explicitly documents this as "Check whether output lock hash type within enabled range," confirming the type script is intentionally (or inadvertently) excluded. The error variant `ScriptHashTypeNotPermitted` in `util/types/src/core/error.rs` line 213 even reads "The **lock** script hash type is not permitted," reinforcing that only the lock path was considered.

`NonContextualTransactionVerifier::verify` (lines 94–102) calls `self.script_hash_type.verify()` as its last step and returns `Ok(())` for such a transaction. The transaction then enters `ContextualTransactionVerifier::verify` (lines 162–171), which runs `time_relative.verify()`, `capacity.verify()`, and finally `self.script.verify(max_cycles)`. Inside `TransactionScriptsVerifier`, `extract_script_and_dep_index` in `script/src/types.rs` (lines 832–860) calls `ScriptHashType::try_from(script.hash_type())` and, for an unknown variant, returns `ScriptError::InvalidScriptHashType`. This is the first point of rejection — after all the contextual checks have already run.

The existing test `test_unknown_hash_type_output_lock` in `verification/src/tests/transaction_verifier.rs` (lines 82–97) covers only the lock script path; there is no corresponding test for the type script path.

## Impact Explanation
The concrete impact is **avoidable CPU and I/O work per malformed transaction**: every such transaction forces the node to run `TimeRelativeTransactionVerifier`, `CapacityVerifier`, and the initial phase of `TransactionScriptsVerifier` (script-group resolution) before rejection. This maps to **Low (501–2000 points): Any other important performance improvements for CKB**. The extra work per transaction is bounded (script-group resolution fails before any VM execution), and the tx-pool rate limiting caps the aggregate throughput of such transactions, so this does not rise to the level of network congestion or node crash.

## Likelihood Explanation
Any unprivileged participant who controls at least one live cell can craft such a transaction. Constructing a `CellOutput` with `lock.hash_type ∈ {0,1,2,4}` and `type.hash_type = 6` requires no special privilege. The attacker is rate-limited by the tx-pool but can sustain a steady stream of such transactions within those limits, each consuming more node resources than a correctly-rejected non-contextual transaction would.

## Recommendation
Extend `ScriptHashTypeVerifier::verify` to also validate the type script's `hash_type` when a type script is present, immediately after the existing lock check inside the output loop:

```rust
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
        }).into());
    }
}
```

Also update the struct comment at line 70 and add a corresponding unit test mirroring `test_unknown_hash_type_output_lock` for the type script path.

## Proof of Concept
Build a `TransactionView` with one output whose `lock.hash_type = 0` (Data) and `type.hash_type = 6` (unknown). Call `ScriptHashTypeVerifier::new(&tx).verify()` — returns `Ok(())`. Call `NonContextualTransactionVerifier::new(&tx, &consensus).verify()` — returns `Ok(())`. Then call `ContextualTransactionVerifier::new(...)` and invoke `.verify(max_cycles, false)` — it runs `time_relative.verify()` and `capacity.verify()` successfully, then enters `TransactionScriptsVerifier::verify`, where `extract_script_and_dep_index` returns `ScriptError::InvalidScriptHashType` for the type script group. The gap is confirmed: rejection occurs only inside contextual verification, not at the cheaper non-contextual gate.