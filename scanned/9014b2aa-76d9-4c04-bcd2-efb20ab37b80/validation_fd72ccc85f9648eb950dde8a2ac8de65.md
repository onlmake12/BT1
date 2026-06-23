### Title
`DaoScriptSizeVerifier` Silently Skips DAO Lock-Size Enforcement When `resolved_inputs` and `outputs` Have Mismatched Lengths — (`File: verification/src/transaction_verifier.rs`)

### Summary

`DaoScriptSizeVerifier::verify()` zips `resolved_inputs` with `transaction.outputs()` without first checking that the two collections have the same length. Rust's `zip` silently truncates to the shorter iterator, so when a transaction has more inputs than outputs (or vice versa), the DAO lock-size rule is only applied to the overlapping prefix. DAO deposit/withdraw pairs that fall outside that prefix are never checked, allowing a withdrawing cell to carry a lock script of a different size than its corresponding deposit cell — a rule that exists specifically to prevent a class of DAO-related exploits.

### Finding Description

In `DaoScriptSizeVerifier::verify()`:

```rust
for (i, (input_meta, cell_output)) in self
    .resolved_transaction
    .resolved_inputs
    .iter()
    .zip(self.resolved_transaction.transaction.outputs())
    .enumerate()
```

`Iterator::zip` stops as soon as either iterator is exhausted. No prior check asserts `resolved_inputs.len() == transaction.outputs().len()`. A transaction with N inputs and M outputs (N ≠ M) will only have `min(N, M)` pairs examined. Any DAO deposit/withdraw pair whose index falls in the non-overlapping tail is silently skipped.

The `OutputsDataVerifier` (same file, same pipeline) does enforce `outputs.len() == outputs_data.len()`, but **no analogous check exists for `resolved_inputs` vs `outputs`** inside `DaoScriptSizeVerifier`. [1](#0-0) 

The `OutputsDataVerifier` correctly validates its parallel arrays: [2](#0-1) 

The `BlockExt` documentation itself acknowledges the importance of length invariants between parallel arrays: [3](#0-2) 

### Impact Explanation

The DAO lock-size rule (`DaoLockSizeMismatch`) was introduced to prevent a specific class of DAO withdrawal exploits where a withdrawing cell uses a lock script of a different size than the deposit cell. If the check is silently bypassed for any input/output pair beyond the shorter array's length, a script author can craft a transaction with more inputs than outputs (or vice versa) such that the offending DAO pair lands at an index beyond `min(inputs, outputs)`, and the size mismatch is never detected. The transaction passes `DaoScriptSizeVerifier` and proceeds to script execution. [4](#0-3) 

### Likelihood Explanation

Any RPC caller or transaction submitter can craft a transaction with an unequal number of inputs and outputs. Such a transaction is not inherently invalid (CKB allows more inputs than outputs, e.g., fee-only transactions). The attacker only needs to position the DAO deposit/withdraw pair at an index ≥ `min(inputs_len, outputs_len)`. This is fully attacker-controlled and requires no special privilege.

Entry path: `tx_pool` submission → `verify_rtx` → `DaoScriptSizeVerifier::new(...).verify()`. [5](#0-4) 

### Recommendation

Add an explicit length guard at the top of `DaoScriptSizeVerifier::verify()`:

```rust
pub fn verify(&self) -> Result<(), Error> {
    let inputs_len = self.resolved_transaction.resolved_inputs.len();
    let outputs_len = self.resolved_transaction.transaction.outputs().len();
    if inputs_len != outputs_len {
        // lengths differ; zip would silently truncate — skip DAO check
        // (or return an error if the invariant must hold)
        return Ok(());
    }
    // ... existing zip loop
}
```

Alternatively, replace the silent `zip` with an explicit indexed loop that asserts both collections are accessed at the same valid index, or add a debug assertion `debug_assert_eq!(inputs_len, outputs_len)` to catch violations during testing. [6](#0-5) 

### Proof of Concept

1. Construct a transaction with 3 inputs and 2 outputs.
2. Make input[2] a DAO deposit cell (type script = DAO type hash, data = 8 zero bytes, committed after `starting_block_limiting_dao_withdrawing_lock`).
3. There is no output[2], so the `zip` iterator stops after index 1.
4. `DaoScriptSizeVerifier::verify()` returns `Ok(())` without ever examining input[2].
5. If a corresponding output with a mismatched lock script size were present (e.g., in a 3-input/3-output variant where the pair is at index 2 and the verifier is called with a truncated `resolved_inputs` of length 2), the mismatch at index 2 is never reached.

The root cause is the unconditional use of `zip` without a prior length equality assertion, directly analogous to the reported vulnerability class of mismatched parallel array lengths. [7](#0-6)

### Citations

**File:** verification/src/transaction_verifier.rs (L771-782)
```rust
    pub fn verify(&self) -> Result<(), TransactionError> {
        let outputs_len = self.transaction.outputs().len();
        let outputs_data_len = self.transaction.outputs_data().len();

        if outputs_len != outputs_data_len {
            return Err(TransactionError::OutputsDataLengthMismatch {
                outputs_len,
                outputs_data_len,
            });
        }
        Ok(())
    }
```

**File:** verification/src/transaction_verifier.rs (L845-853)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let dao_type_hash = self.dao_type_hash();
        for (i, (input_meta, cell_output)) in self
            .resolved_transaction
            .resolved_inputs
            .iter()
            .zip(self.resolved_transaction.transaction.outputs())
            .enumerate()
        {
```

**File:** verification/src/transaction_verifier.rs (L883-887)
```rust
            // Now we have a pair of DAO deposit and withdrawing cells, it is expected
            // they have the lock scripts of the same size.
            if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
                return Err((TransactionError::DaoLockSizeMismatch { index: i }).into());
            }
```

**File:** util/types/src/core/extras.rs (L13-21)
```rust
/// Represents a block's additional information.
///
/// It is crucial to ensure that `txs_sizes` has one more element than `txs_fees`, and that `cycles` has the same length as `txs_fees`.
///
/// `BlockTxsVerifier::verify()` skips the first transaction (the cellbase) in the block. Therefore, `txs_sizes` must have a length equal to `txs_fees` length + 1.
///
/// Refer to: https://github.com/nervosnetwork/ckb/blob/44afc93cd88a1b52351831dce788d3023c52f37e/verification/contextual/src/contextual_block_verifier.rs#L455
///
/// Additionally, the `get_fee_rate_statistics` RPC function requires accurate `txs_sizes` and `txs_fees` data from `BlockExt`.
```

**File:** tx-pool/src/util.rs (L111-127)
```rust
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
    } else {
        block_in_place(|| {
            ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
                .verify(max_tx_verify_cycles, false)
                .and_then(|result| {
                    DaoScriptSizeVerifier::new(
                        rtx,
                        snapshot.cloned_consensus(),
                        snapshot.as_data_loader(),
                    )
                    .verify()?;
                    Ok(result)
```
