Audit Report

## Title
`ScriptHashTypeVerifier` Skips Type Script `hash_type` Validation, Enabling Unpunished Tx-Pool Spam — (`verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and enforces the `ENABLED_SCRIPT_HASH_TYPE` constraint exclusively on the lock script, never inspecting the type script. A transaction whose lock script carries a permitted `hash_type` (e.g., `Data = 0`) but whose type script carries a consensus-disabled `hash_type` (e.g., `Data3 = 6`) passes non-contextual verification, enters the tx pool, and fails only at contextual script verification. Because the non-contextual gate never fires `ScriptHashTypeNotPermitted` for the type script, the submitting peer is never classified as malformed and is never banned, creating a repeatable, fee-bounded tx-pool pollution vector.

## Finding Description
`ScriptHashTypeVerifier::verify()` at `verification/src/transaction_verifier.rs` lines 796–814 loops over outputs and checks only `output.lock().hash_type()`:

```rust
if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
    let val: u8 = hash_type.into();
    if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) { … }
} else { … }
// output.type_() is never examined
```

`ENABLED_SCRIPT_HASH_TYPE` in `util/constant/src/consensus.rs` lines 7–12 contains `{0, 1, 2, 4}`; byte value `6` (`Data3`) is absent.

`ScriptHashType` is generated via `seq!(N in 3..=127 { Data~N = N << 1 })` in `util/gen-types/src/core.rs` lines 9–33, so `Data3 = 6` is a fully valid enum variant. `verify_value(6)` returns `true` because `6.is_multiple_of(2)` holds (`util/gen-types/src/core.rs` line 40), meaning the molecule-level `CellOutputReader::check_data()` (`util/gen-types/src/extension/check_data.rs` line 26) accepts `Data3` in a type script. `TryInto::<ScriptHashType>::try_into(6u8)` also succeeds via `from_repr`, so the verifier's `if let Ok(hash_type)` branch is taken for the lock script — but the type script is never reached.

`NonContextualTransactionVerifier` composes `ScriptHashTypeVerifier` as its sole hash-type gate (`verification/src/transaction_verifier.rs` lines 71–101). `tx-pool/src/util.rs` lines 56–83 calls this verifier as the first admission check. A transaction with `Data3` in the type script passes all of this.

Contextual verification eventually calls `select_version` (`script/src/types.rs` lines 900–936), where `Data3` falls into the catch-all arm and returns `Err(ScriptError::InvalidScriptHashType(...))`. This error is a `ScriptError`, not a `TransactionError::ScriptHashTypeNotPermitted`, so `is_malformed_tx()` (`util/types/src/core/error.rs` lines 244–264) returns `false`. The peer-banning gate in `tx-pool/src/process.rs` lines 318–333 only bans on `is_malformed_tx()` from non-contextual verification, so the submitter is never penalized.

## Impact Explanation
This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** An unprivileged attacker can continuously submit transactions with a valid lock script and a `Data3` type script. Each transaction bypasses the cheap non-contextual gate, occupies a tx-pool slot, and triggers contextual verification before being evicted. The submitter is never banned, so the attack is repeatable at the cost of minimum fee rates only. Sustained submission can fill the tx pool with permanently-failing transactions, crowding out legitimate traffic and degrading node throughput.

## Likelihood Explanation
The `send_transaction` RPC endpoint is fully unprivileged. Constructing a `CellOutput` with `hash_type = 6` in the type script requires no special access — it is a single byte field. The molecule `check_data()` accepts it, the non-contextual verifier passes it, and no rate-limiting beyond the fee floor applies. The attack is trivially scriptable and indefinitely repeatable since no banning occurs.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to validate the type script's `hash_type` when a type script is present, mirroring the existing lock-script logic:

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

This is consistent with `CellOutputReader::check_data()` which already validates both lock and type scripts symmetrically. A corresponding test `test_not_enabled_hash_type_output_type` should be added alongside the existing `test_not_enabled_hash_type_output_lock`.

## Proof of Concept
1. Build a `CellOutput` with lock `hash_type = Data` (byte `0`, permitted) and type script `hash_type = Data3` (byte `6`, not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Wrap it in a `TransactionView` and call `ScriptHashTypeVerifier::new(&tx).verify()`.
3. Observe `Ok(())` — the non-permitted type script hash type is not caught.
4. Submit via `send_transaction` RPC; the transaction enters the pool, contextual verification calls `select_version` which returns `Err(ScriptError::InvalidScriptHashType(...))`, the transaction is rejected, but `is_malformed_tx()` returns `false` and the peer is not banned.
5. Repeat indefinitely; the node processes each submission through contextual verification without ever banning the caller.

The existing test `test_not_enabled_hash_type_output_lock` at `verification/src/tests/transaction_verifier.rs` lines 100–122 confirms the lock-script path is covered; no analogous test for the type-script path exists, confirming the gap.