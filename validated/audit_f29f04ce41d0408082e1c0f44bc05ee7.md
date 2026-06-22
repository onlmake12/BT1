### Title
`DaoScriptSizeVerifier::verify()` Positional `zip` Pairing Allows Bypass of RFC0044 DAO Lock Script Size Constraint — (File: `verification/src/transaction_verifier.rs`)

### Summary
`DaoScriptSizeVerifier::verify()` pairs transaction inputs with outputs strictly by position using `Iterator::zip`. A transaction sender can place a DAO deposit cell and its corresponding DAO withdraw cell at *different* indices, causing neither positional pair to satisfy the "both must have DAO type script" guard, and the lock-script-size check is silently skipped for the entire transaction. Because the same verifier is invoked in both the tx-pool path and the block-verification path, the bypass is end-to-end.

### Finding Description

In `verification/src/transaction_verifier.rs`, `DaoScriptSizeVerifier::verify()` iterates:

```rust
for (i, (input_meta, cell_output)) in self
    .resolved_transaction
    .resolved_inputs
    .iter()
    .zip(self.resolved_transaction.transaction.outputs())
    .enumerate()
{
    // Both the input and output cell must use Nervos DAO as type script
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
``` [1](#0-0) 

`zip` terminates at the shorter iterator and pairs `input[i]` with `output[i]`. The guard on line 855–858 requires **both** the input and the output at the same index to carry the DAO type script. If a DAO deposit cell sits at `input[1]` and the corresponding DAO withdraw cell sits at `output[0]`, the pairs evaluated are:

| pair | input DAO? | output DAO? | result |
|------|-----------|------------|--------|
| `(input[0], output[0])` | No | Yes | `continue` |
| `(input[1], output[1])` | Yes | No | `continue` |

Neither pair triggers the size comparison. The lock-script-size mismatch is never detected.

The same `DaoScriptSizeVerifier` struct is called in the **tx-pool** path:

```rust
} else if let Some(command_rx) = command_rx {
    ...
    .and_then(|result| {
        DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
            .verify()?;
        Ok(result)
    })
``` [2](#0-1) 

And in the **block verifier** path:

```rust
.and_then(|result| {
    if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
        DaoScriptSizeVerifier::new(
            Arc::clone(tx),
            Arc::clone(&self.context.consensus),
            self.context.store.as_data_loader(),
        ).verify()?;
    }
    Ok(result)
})
``` [3](#0-2) 

Both call sites share the same flawed positional-zip logic, so the bypass propagates through tx-pool admission **and** block acceptance.

A secondary, independent gap exists in `verify_rtx`: when a cache entry is present, only `TimeRelativeTransactionVerifier` is run and `DaoScriptSizeVerifier` is skipped entirely in the tx-pool path, even though the block verifier does run it for cached transactions. [4](#0-3) 

### Impact Explanation

The code comment explicitly states:

> "Verifies that deposit cell and withdrawing cell in Nervos DAO use same sized lock scripts. **It provides a temporary solution till Nervos DAO script can be properly upgraded.**" [5](#0-4) 

This confirms the on-chain DAO script does **not** itself enforce lock-script-size equality; `DaoScriptSizeVerifier` is the sole enforcement layer for RFC0044. Bypassing it allows a depositor to use a large lock script at deposit time and a small lock script at withdraw time (or vice versa), manipulating the occupied-capacity accounting of the DAO cell. Because the DAO interest calculation is based on the total capacity of the deposit cell and the on-chain DAO script does not re-verify lock-script size, the attacker can extract capacity that was previously attributed to the lock script's occupied space, constituting a cell/capacity accounting violation.

### Likelihood Explanation

Any unprivileged transaction sender can craft such a transaction. No special role, key, or majority hash power is required. The only prerequisite is owning a DAO deposit cell and constructing a withdrawal transaction with the deposit cell at a different index than the withdraw cell. This is a standard transaction construction choice fully under the sender's control.

### Recommendation

Replace the positional `zip` pairing with a semantic scan. For each input that is a DAO deposit cell (DAO type script + all-zero data + committed after the limiting block), search **all** outputs for a DAO withdraw cell and compare lock-script sizes regardless of index. Alternatively, build a map from DAO type-script hash to (input index, lock size) and then scan all outputs for matching DAO type scripts.

### Proof of Concept

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

`DaoScriptSizeVerifier::verify()` returns `Ok(())`. The lock-script size mismatch between `input[1]` (empty args, size S₁) and `output[0]` (20-byte args, size S₂ > S₁) is never checked. The transaction passes both tx-pool admission and block verification, violating the RFC0044 constraint.

### Citations

**File:** verification/src/transaction_verifier.rs (L817-818)
```rust
/// Verifies that deposit cell and withdrawing cell in Nervos DAO use same sized lock scripts.
/// It provides a temporary solution till Nervos DAO script can be properly upgraded.
```

**File:** verification/src/transaction_verifier.rs (L845-890)
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
            // Both the input and output cell must use Nervos DAO as type script
            if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
                && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
            {
                continue;
            }

            // A Nervos DAO deposit cell must have input data
            let input_data = match self.data_loader.load_cell_data(input_meta) {
                Some(data) => data,
                None => continue,
            };

            // Only input data with full zeros are counted as deposit cell
            if input_data.into_iter().any(|b| b != 0) {
                continue;
            }

            // Only cells committed after the pre-defined block number in consensus is
            // applied to this rule
            if let Some(info) = &input_meta.transaction_info
                && info.block_number
                    < self
                        .consensus
                        .starting_block_limiting_dao_withdrawing_lock()
            {
                continue;
            }

            // Now we have a pair of DAO deposit and withdrawing cells, it is expected
            // they have the lock scripts of the same size.
            if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
                return Err((TransactionError::DaoLockSizeMismatch { index: i }).into());
            }
        }
        Ok(())
    }
```

**File:** tx-pool/src/util.rs (L96-100)
```rust
    if let Some(completed) = cache_entry {
        TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
            .verify()
            .map(|_| *completed)
            .map_err(Reject::Verification)
```

**File:** tx-pool/src/util.rs (L101-115)
```rust
    } else if let Some(command_rx) = command_rx {
        ContextualTransactionVerifier::new(
            Arc::clone(&rtx),
            consensus,
            data_loader,
            Arc::clone(&tx_env),
        )
        .verify_with_pause(max_tx_verify_cycles, command_rx)
        .await
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L444-453)
```rust
                }.and_then(|result| {
                    if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
                        DaoScriptSizeVerifier::new(
                            Arc::clone(tx),
                            Arc::clone(&self.context.consensus),
                            self.context.store.as_data_loader(),
                        ).verify()?;
                    }
                    Ok(result)
                })
```
