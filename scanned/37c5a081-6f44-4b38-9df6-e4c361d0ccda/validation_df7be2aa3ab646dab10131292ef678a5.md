The code confirms the claim. Let me verify the tx-pool admission path:

Audit Report

## Title
`ScriptHashTypeVerifier::verify()` Skips Type Script Hash Type Validation, Enabling Tx-Pool Pollution — (`File: verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates only the **lock** script's `hash_type` against `ENABLED_SCRIPT_HASH_TYPE`, leaving the **type** script's `hash_type` unchecked. An attacker can submit a transaction whose type script carries a non-enabled `ScriptHashType` (e.g., `Data3` = 6), bypass this non-contextual gate, and have the transaction admitted to the tx-pool. The transaction is only rejected during expensive script execution, wasting node and miner resources and enabling tx-pool slot exhaustion.

## Finding Description

`ENABLED_SCRIPT_HASH_TYPE` in `util/constant/src/consensus.rs` (L7–11) defines the permitted set `{0, 1, 2, 4}` (`Data`, `Type`, `Data1`, `Data2`).

`ScriptHashTypeVerifier::verify()` in `verification/src/transaction_verifier.rs` (L796–814) iterates outputs and checks only `output.lock().hash_type()`:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) { ... }
        } else { ... }
    }
    Ok(())
}
```

`output.type_()` is never consulted.

The `ScriptHashType` enum in `util/gen-types/src/core.rs` (L9–41) generates `Data3`…`Data127` (values 6, 8, …, 254) via `seq!`. `ScriptHashType::verify_value()` (L39–41) returns `true` for any even value or 1, so `Data3` (6) is structurally valid. `CellOutputReader::check_data()` in `util/gen-types/src/extension/check_data.rs` (L24–27) checks both lock and type scripts for structural validity only — not against the enabled set — so a type script with `hash_type = 6` passes P2P/RPC ingress checks.

The tx-pool admission path in `tx-pool/src/util.rs` (L56–83) calls `NonContextualTransactionVerifier::new(tx, consensus).verify()`, which includes `ScriptHashTypeVerifier`. This is the only non-contextual gate. The contextual path (`verify_rtx`, L85–132) runs `ContextualTransactionVerifier`, which invokes script execution. Inside `select_version()` in `script/src/types.rs` (L930–935), any non-enabled hash type returns `ScriptError::InvalidScriptHashType` — but only after VM setup has already been initiated.

**Exploit path:**
1. Attacker constructs a transaction with a valid lock script (`Data`/`Type`) and a type script with `hash_type = 6` (`Data3`), paying the minimum fee.
2. Submits via `send_transaction` RPC.
3. `check_data` passes (6 is even → structurally valid).
4. `ScriptHashTypeVerifier::verify()` passes (only checks lock script).
5. Transaction enters the tx-pool.
6. Miner picks it up; `select_version()` returns `InvalidScriptHashType` → transaction evicted after wasted VM work.
7. Attacker repeats to exhaust tx-pool slots and miner CPU.

The test `test_not_enabled_hash_type_output_lock` in `verification/src/tests/transaction_verifier.rs` (L100–122) covers only the lock script case; no counterpart exists for the type script, confirming the gap.

## Impact Explanation

This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** Attacker-crafted transactions occupy tx-pool memory slots, displace legitimate transactions, and force miners to expend CPU on VM initialization and script execution for transactions that should have been rejected non-contextually. There is no consensus split or asset theft; all nodes ultimately reject such transactions during script execution.

## Likelihood Explanation

Any actor with RPC access can submit transactions. The only cost is the minimum transaction fee in CKB (enforced by `check_tx_fee` in `tx-pool/src/util.rs` L45–52). The `ScriptHashType` encoding scheme is public. The attack is repeatable at scale with modest CKB holdings, and requires no keys, stake, or special role beyond RPC access.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type for each output, mirroring the existing lock-script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock check
        self.check_hash_type(output.lock().hash_type())?;
        // add type script check
        if let Some(type_script) = output.type_().to_opt() {
            self.check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}
```

This mirrors `CellOutputReader::check_data()` in `util/gen-types/src/extension/check_data.rs` (L24–27), which already validates both lock and type scripts for structural validity. A corresponding test analogous to `test_not_enabled_hash_type_output_lock` should be added for the type script case.

## Proof of Concept

```rust
#[test]
pub fn test_not_enabled_hash_type_output_type_script() {
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .lock(Script::default().as_builder()
                    .hash_type(ScriptHashType::Data)  // enabled
                    .build())
                .type_(Some(Script::default().as_builder()
                    .hash_type(ScriptHashType::Data3)  // value=6, NOT in ENABLED_SCRIPT_HASH_TYPE
                    .build()).pack())
                .build(),
        )
        .build();
    let verifier = ScriptHashTypeVerifier::new(&transaction);
    // Currently passes — type script hash type is never checked
    assert!(verifier.verify().is_ok()); // BUG: should return Err
}
```

Running this test against the current code demonstrates the gap: `verify()` returns `Ok(())` despite the type script carrying a non-enabled hash type. The transaction would then pass `non_contextual_verify` in `tx-pool/src/util.rs` (L56–83) and enter the tx-pool, only to be rejected later in `select_version()` at `script/src/types.rs` (L930–935).