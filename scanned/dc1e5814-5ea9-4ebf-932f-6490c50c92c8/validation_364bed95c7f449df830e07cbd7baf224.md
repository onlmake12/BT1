Audit Report

## Title
`ScriptHashTypeVerifier` Omits `hash_type` Validation for Type Scripts on Transaction Outputs — (`File: verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` iterates over every transaction output and validates `hash_type` only on the **lock** script, never on the optional **type** script. A transaction whose type script carries a non-permitted or structurally invalid `hash_type` byte passes `NonContextualTransactionVerifier` entirely, enters the tx-pool, and is propagated to peers before being rejected at contextual verification. Because the attacker's inputs are never consumed (the transaction is rejected before mining), the same cells can be reused to spam the network at near-zero cost.

## Finding Description
`ScriptHashTypeVerifier::verify()` (lines 796–814 of `verification/src/transaction_verifier.rs`) loops over outputs and calls only `output.lock().hash_type()`:

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

`output.type_()` is never called. `ENABLED_SCRIPT_HASH_TYPE` (in `util/constant/src/consensus.rs`) permits only `{0, 1, 2, 4}`. Any type script carrying a byte outside this set — e.g., `3` (not a valid `ScriptHashType` variant) or `6` (`Data3`, a valid variant but not in the enabled set) — passes this verifier without error.

`NonContextualTransactionVerifier::verify()` (lines 94–102) calls `self.script_hash_type.verify()` as its final gate; no other non-contextual check covers type script `hash_type`.

At contextual verification, `TxInfo::extract_script_and_dep_index()` (`script/src/types.rs`, lines 832–860) does call `ScriptHashType::try_from(script.hash_type())` and returns `ScriptError::InvalidScriptHashType` for unknown or non-permitted values — so the transaction is ultimately rejected. However, this rejection happens **after** the transaction has already entered the tx-pool and been relayed to peers.

The `check_data` path (`util/gen-types/src/extension/check_data.rs`, lines 48–54) only validates output count vs. output-data count and `dep_type` on cell-deps; it does not inspect script `hash_type` fields. The `outputs_validator` RPC parameter (`rpc/src/module/pool.rs`, lines 499–526) only validates well-known script code hashes, not `hash_type` values.

## Impact Explanation
Because the attacker's transaction is rejected at contextual verification (not mined), the attacker's input cells are never consumed. The attacker can therefore reuse the same UTXOs to submit an unbounded stream of crafted transactions, each of which passes non-contextual verification, occupies a tx-pool slot, and is relayed to peers before rejection. This constitutes **network congestion with few costs** — matching the High-severity allowed impact class. Every full node in the network must perform contextual verification work and relay bandwidth for each such transaction.

## Likelihood Explanation
The exploit requires only submitting a transaction via the `send_transaction` RPC with `outputs_validator = "passthrough"` or via the P2P relay protocol. No key material, privilege, or hash-power is needed. Crafting the transaction is trivial: set the `hash_type` byte of any output's type script to a value not in `{0, 1, 2, 4}`. Because rejected transactions do not consume inputs, the attacker's cost is bounded only by bandwidth and the RPC rate limit, making the attack repeatable at near-zero marginal cost.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to also validate the `hash_type` of each output's type script when present, mirroring the existing lock-script check:

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
        // NEW: type script check
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

## Proof of Concept
1. Obtain any live UTXO (cell) on a CKB testnet node.
2. Build a transaction spending that cell with one output whose **lock** script uses `hash_type = 0x00` (valid `Data`) and whose **type** script uses `hash_type = 0x03` (not a valid `ScriptHashType` variant, not in `ENABLED_SCRIPT_HASH_TYPE`).
3. Submit via `send_transaction` RPC with `outputs_validator = "passthrough"`.
4. Observe that `NonContextualTransactionVerifier::verify()` → `ScriptHashTypeVerifier::verify()` returns `Ok(())` and the transaction enters the pool.
5. Confirm the transaction is subsequently rejected at contextual verification with `ScriptError::InvalidScriptHashType`.
6. Resubmit a slightly varied version of the same transaction (e.g., different witness) using the same input cell — confirm it again passes non-contextual verification, demonstrating the zero-cost repeatability.
7. For comparison, place `hash_type = 0x03` on the **lock** script instead: confirm `ScriptHashTypeVerifier` immediately rejects it with `TransactionError::InvalidScriptHashType`, proving the asymmetric gap.