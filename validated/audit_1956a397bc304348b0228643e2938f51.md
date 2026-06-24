Audit Report

## Title
`DaoScriptSizeVerifier::verify()` Positional `zip` Pairing Silently Skips RFC0044 Lock-Script-Size Check When DAO Input and Output Indices Differ — (File: `verification/src/transaction_verifier.rs`)

## Summary
`DaoScriptSizeVerifier::verify()` pairs transaction inputs with outputs strictly by position using `Iterator::zip`. An attacker can place a DAO deposit cell at `input[i]` and the corresponding DAO withdraw cell at `output[j]` where `i ≠ j`, causing neither positional pair to satisfy the "both must carry DAO type script" guard. The lock-script-size comparison is silently skipped, and the transaction passes both tx-pool admission and block verification, violating the sole enforcement layer for RFC0044.

## Finding Description
In `verification/src/transaction_verifier.rs`, `DaoScriptSizeVerifier::verify()` iterates:

```rust
for (i, (input_meta, cell_output)) in self
    .resolved_transaction
    .resolved_inputs
    .iter()
    .zip(self.resolved_transaction.transaction.outputs())
    .enumerate()
{
    if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
        && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
    {
        continue;
    }
    ...
    if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
        return Err((TransactionError::DaoLockSizeMismatch { index: i }).into());
    }
}
```

`zip` terminates at the shorter iterator and pairs `input[i]` with `output[i]`. The guard at lines 855–858 requires **both** the input and the output at the same index to carry the DAO type script. If a DAO deposit cell sits at `input[1]` and the corresponding DAO withdraw cell sits at `output[0]`, the evaluated pairs are:

| pair | input DAO? | output DAO? | result |
|------|-----------|------------|--------|
| `(input[0], output[0])` | No | Yes | `continue` |
| `(input[1], output[1])` | Yes | No | `continue` |

Neither pair triggers the size comparison. The lock-script-size mismatch is never detected, and `verify()` returns `Ok(())`.

This verifier is the **sole** enforcement layer for RFC0044, as the code comment explicitly states it is "a temporary solution till Nervos DAO script can be properly upgraded." The on-chain DAO script does not itself re-verify lock-script size.

The same flawed verifier is called in both the tx-pool path (lines 111–113 of `tx-pool/src/util.rs`) and the block verifier path (lines 446–450 of `verification/contextual/src/contextual_block_verifier.rs`), so the bypass propagates end-to-end.

A secondary gap exists: when a `cache_entry` is present in `verify_rtx` (lines 96–100 of `tx-pool/src/util.rs`), only `TimeRelativeTransactionVerifier` is run and `DaoScriptSizeVerifier` is skipped entirely in the tx-pool path, even though the block verifier does run it for cached transactions.

## Impact Explanation
The DAO interest calculation in `DaoCalculator::calculate_maximum_withdraw` (`util/dao/src/lib.rs`, lines 149–156) computes:

```rust
let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar) / u128::from(deposit_ar);
let withdraw_capacity = Capacity::shannons(withdraw_counted_capacity as u64)
    .safe_add(occupied_capacity)?;
```

Here `output` is the **withdrawal cell**. If the withdrawal cell has a smaller lock script than the deposit cell, its `occupied_capacity` is smaller, so `counted_capacity` (the capacity that earns interest) is larger. The on-chain DAO script uses this same formula and will permit the inflated withdrawal. The attacker extracts capacity beyond what they deposited plus legitimate interest — a concrete CKB economy violation. This matches the allowed impact: **Vulnerabilities which could easily damage CKB economy (Critical)**.

## Likelihood Explanation
Any unprivileged transaction sender owning a DAO deposit cell can trigger this. The only required action is constructing a withdrawal transaction with the DAO deposit cell at a different input index than the DAO withdraw cell at the output index. This is a standard transaction construction choice fully under the sender's control, requires no special role, key, or majority hash power, and is repeatable at will.

## Recommendation
Replace the positional `zip` pairing with a semantic scan. For each input that qualifies as a DAO deposit cell (DAO type script + all-zero data + committed after `starting_block_limiting_dao_withdrawing_lock`), search **all** outputs for a DAO withdraw cell (DAO type script) and compare lock-script sizes regardless of index. Alternatively, build a map from DAO type-script hash to `(input_index, lock_size)` and then scan all outputs for matching DAO type scripts to perform the size comparison.

## Proof of Concept
Craft a transaction with:

```
input[0]:  regular cell          (no DAO type script, capacity = 50 CKB)
input[1]:  DAO deposit cell      (DAO type script, data = [0;8], lock args = [],  capacity = 201 CKB)
output[0]: DAO withdraw cell     (DAO type script, data = [block_num], lock args = [1;20], capacity = 200 CKB)
output[1]: regular cell          (no DAO type script, capacity = 50 CKB)
```

`zip` produces two pairs:
- `(input[0], output[0])`: `input[0]` has no DAO type script → `continue`
- `(input[1], output[1])`: `output[1]` has no DAO type script → `continue`

`DaoScriptSizeVerifier::verify()` returns `Ok(())`. The lock-script size mismatch between `input[1]` (empty args) and `output[0]` (20-byte args) is never checked. The transaction passes both tx-pool admission and block verification. At phase 2 withdrawal, the smaller lock script on the withdrawal cell inflates `counted_capacity`, allowing the attacker to claim more capacity than legitimately earned, violating the RFC0044 constraint.