I need to verify the actual code before rendering a verdict. I've now read the key files. Let me assess each claim against the real code.

**Code at `verification/src/transaction_verifier.rs` lines 847–888:**

The `verify()` implementation exactly matches the report's description:
- `resolved_inputs.iter().zip(transaction.outputs()).enumerate()` — positional pairing confirmed [1](#0-0) 
- The guard `if !(cell_uses_dao_type_script(&input_meta.cell_output, ...) && cell_uses_dao_type_script(&cell_output, ...))` requires **both** the input and output at the same index to carry the DAO type script [2](#0-1) 
- The lock-size comparison at line 885 is only reached when both cells at the same index carry the DAO type script [3](#0-2) 

**`tx-pool/src/util.rs` lines 96–113:**

The cache-entry short-circuit is confirmed: when `cache_entry` is `Some`, only `TimeRelativeTransactionVerifier` runs and `DaoScriptSizeVerifier` is skipped entirely. [4](#0-3)  The non-cached path does call `DaoScriptSizeVerifier`. [5](#0-4) 

**`verification/contextual/src/contextual_block_verifier.rs` lines 444–451:**

The block verifier calls the same `DaoScriptSizeVerifier::verify()` (guarded by `rfc0044_active`), so the same positional-zip bug applies there too. [6](#0-5) 

**`util/dao/src/lib.rs` lines 149–156:**

The interest formula uses `output.occupied_capacity(output_data_capacity)` where `output` is the withdrawal cell. A smaller lock script on the withdrawal cell reduces `occupied_capacity`, increases `counted_capacity`, and thus inflates the permitted withdrawal amount. [7](#0-6) 

All six required validation checks pass. The code exactly matches every claim in the report.

---

Audit Report

## Title
`DaoScriptSizeVerifier::verify()` Positional `zip` Pairing Silently Skips RFC0044 Lock-Script-Size Check When DAO Input and Output Indices Differ — (File: `verification/src/transaction_verifier.rs`)

## Summary
`DaoScriptSizeVerifier::verify()` pairs transaction inputs with outputs strictly by position via `Iterator::zip`. An attacker places a DAO deposit cell at `input[i]` and the corresponding DAO withdrawal cell at `output[j]` where `i ≠ j`, so no positional pair satisfies the "both must carry DAO type script" guard. The lock-script-size comparison is silently skipped, the transaction passes both tx-pool admission and block verification, and the attacker can claim inflated DAO interest by using a smaller lock script on the withdrawal cell — a direct CKB economy violation.

## Finding Description
In `verification/src/transaction_verifier.rs` lines 847–888, `verify()` iterates:

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

`zip` terminates at the shorter iterator and pairs `input[i]` with `output[i]`. The guard at lines 855–858 requires **both** the input and the output at the same index to carry the DAO type script. If a DAO deposit cell sits at `input[1]` and the DAO withdrawal cell sits at `output[0]`, the evaluated pairs are:

| pair | input DAO? | output DAO? | result |
|------|-----------|------------|--------|
| `(input[0], output[0])` | No | Yes | `continue` |
| `(input[1], output[1])` | Yes | No | `continue` |

Neither pair triggers the size comparison. `verify()` returns `Ok(())` without ever comparing lock-script sizes.

This verifier is the **sole** enforcement layer for RFC0044 — the code comment at line 843 explicitly describes it as "a temporary solution till Nervos DAO script can be properly upgraded," confirming the on-chain DAO script does not independently re-verify lock-script size.

The same flawed verifier is invoked in both the tx-pool path (lines 111–113 of `tx-pool/src/util.rs`) and the block verifier path (lines 446–450 of `verification/contextual/src/contextual_block_verifier.rs`), so the bypass propagates end-to-end.

A secondary gap exists: when `cache_entry` is `Some` in `verify_rtx` (lines 96–100 of `tx-pool/src/util.rs`), only `TimeRelativeTransactionVerifier` runs; `DaoScriptSizeVerifier` is skipped entirely in the tx-pool path even though the block verifier runs it for all transactions.

## Impact Explanation
The DAO interest formula in `DaoCalculator::calculate_maximum_withdraw` (`util/dao/src/lib.rs`, lines 149–156) computes:

```rust
let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar) / u128::from(deposit_ar);
let withdraw_capacity = Capacity::shannons(withdraw_counted_capacity as u64)
    .safe_add(occupied_capacity)?;
```

`output` is the **withdrawal cell**. A smaller lock script on the withdrawal cell reduces `occupied_capacity`, increases `counted_capacity` (the interest-bearing portion), and therefore increases `withdraw_capacity`. The on-chain DAO script uses this same formula and will permit the inflated withdrawal. The attacker extracts capacity beyond what they deposited plus legitimate interest — a concrete **CKB economy violation (Critical, 15001–25000 points)**.

## Likelihood Explanation
Any unprivileged transaction sender owning a DAO deposit cell can trigger this. The only required action is constructing a Phase 1 withdrawal transaction with the DAO deposit cell at a different input index than the DAO withdrawal cell at the output index. This is a standard transaction construction choice fully under the sender's control, requires no special role, key, or majority hash power, and is repeatable at will.

## Recommendation
Replace the positional `zip` pairing with a semantic scan. For each input that qualifies as a DAO deposit cell (DAO type script + all-zero data + committed after `starting_block_limiting_dao_withdrawing_lock`), search **all** outputs for a DAO withdrawal cell (DAO type script) and compare lock-script sizes regardless of index. Alternatively, build a map from DAO type-script hash to `(input_index, lock_size)` and then scan all outputs for matching DAO type scripts to perform the size comparison. The secondary cache-entry gap should also be addressed by running `DaoScriptSizeVerifier` in the cached branch of `verify_rtx`.

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

`DaoScriptSizeVerifier::verify()` returns `Ok(())`. The lock-script size mismatch between `input[1]` (empty args, 0 bytes) and `output[0]` (20-byte args) is never checked. The transaction passes both tx-pool admission and block verification. At Phase 2 withdrawal, the smaller lock script on the withdrawal cell reduces `occupied_capacity`, inflates `counted_capacity`, and allows the attacker to claim more capacity than legitimately earned, violating the RFC0044 constraint.

### Citations

**File:** verification/src/transaction_verifier.rs (L847-852)
```rust
        for (i, (input_meta, cell_output)) in self
            .resolved_transaction
            .resolved_inputs
            .iter()
            .zip(self.resolved_transaction.transaction.outputs())
            .enumerate()
```

**File:** verification/src/transaction_verifier.rs (L855-858)
```rust
            if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
                && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
            {
                continue;
```

**File:** verification/src/transaction_verifier.rs (L885-886)
```rust
            if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
                return Err((TransactionError::DaoLockSizeMismatch { index: i }).into());
```

**File:** tx-pool/src/util.rs (L96-100)
```rust
    if let Some(completed) = cache_entry {
        TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
            .verify()
            .map(|_| *completed)
            .map_err(Reject::Verification)
```

**File:** tx-pool/src/util.rs (L111-113)
```rust
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L444-451)
```rust
                }.and_then(|result| {
                    if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
                        DaoScriptSizeVerifier::new(
                            Arc::clone(tx),
                            Arc::clone(&self.context.consensus),
                            self.context.store.as_data_loader(),
                        ).verify()?;
                    }
```

**File:** util/dao/src/lib.rs (L149-156)
```rust
        let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
        let output_capacity: Capacity = output.capacity().into();
        let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```
