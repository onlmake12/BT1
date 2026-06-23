### Title
Silent Bypass of DAO Lock Script Size Enforcement via Positional Zip Without Length Guard in `DaoScriptSizeVerifier` — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`DaoScriptSizeVerifier::verify()` zips `resolved_inputs` with `transaction.outputs()` positionally, with no assertion that the DAO deposit input and its corresponding DAO withdraw output occupy the same array index. Because Rust's `zip` silently stops at the shorter iterator, a transaction sender can place a DAO deposit cell at input index `i` and the DAO withdraw cell at output index `j ≠ i`, ensuring no `(DAO deposit, DAO withdraw)` pair is ever formed and the lock-script-size check is silently skipped entirely.

---

### Finding Description

In `DaoScriptSizeVerifier::verify()`:

```rust
for (i, (input_meta, cell_output)) in self
    .resolved_transaction
    .resolved_inputs
    .iter()
    .zip(self.resolved_transaction.transaction.outputs())
    .enumerate()
``` [1](#0-0) 

The verifier assumes `resolved_inputs[i]` is the DAO deposit cell that corresponds to `outputs[i]` (the DAO withdraw cell). It checks that both cells carry the DAO type script and that their lock script sizes match: [2](#0-1) 

Three structural problems exist simultaneously:

1. `resolved_inputs` and `transaction.outputs()` are independent collections with no guaranteed length relationship — inputs and outputs of a CKB transaction are entirely independent arrays.
2. Rust's `zip` silently truncates to the shorter iterator with no error, no panic, and no warning.
3. If the DAO deposit input is at index `i` and the DAO withdraw output is at index `j ≠ i`, the pair is never formed and the size check is never executed.

**Concrete bypass transaction structure:**

| Slot | Field | Cell type |
|---|---|---|
| `inputs[0]` | non-DAO cell | — |
| `inputs[1]` | DAO deposit cell (lock script size **S**) | DAO |
| `outputs[0]` | DAO withdraw cell (lock script size **L ≠ S**) | DAO |
| `outputs[1]` | non-DAO cell | — |

The zip produces exactly two pairs:
- `(inputs[0], outputs[0])` = (non-DAO, DAO withdraw) → `continue` (not both DAO)
- `(inputs[1], outputs[1])` = (DAO deposit, non-DAO) → `continue` (not both DAO)

The DAO deposit and DAO withdraw cells are never compared. `DaoLockSizeMismatch` is never returned. The transaction passes the verifier.

The same bypass works with unequal lengths: e.g., 1 input (DAO deposit at index 0) and 2 outputs (non-DAO at index 0, DAO withdraw at index 1) — the zip stops after one iteration and `outputs[1]` is never examined.

---

### Impact Explanation

`DaoScriptSizeVerifier` is explicitly described as *"a temporary solution till Nervos DAO script can be properly upgraded"*: [3](#0-2) 

This means the on-chain DAO script itself does **not** enforce lock-script-size equality between deposit and withdraw cells. `DaoScriptSizeVerifier` is the **sole** enforcement layer for this constraint. Bypassing it allows a transaction sender to change their lock script to one of a different serialized size during DAO withdrawal phase 1 — the exact scenario the verifier was introduced to prevent.

The check is enforced at both tx-pool admission: [4](#0-3) 

and at block verification (when `rfc0044` is active): [5](#0-4) 

A successful bypass therefore affects both mempool admission and consensus-level block validity.

---

### Likelihood Explanation

Any unprivileged transaction sender can craft such a transaction. No special privileges, leaked keys, majority hash power, or social engineering are required. The attacker only needs to order their inputs and outputs so that the DAO deposit input and DAO withdraw output are at different positional indices — a trivial structural choice when constructing a transaction.

---

### Recommendation

Replace the positional zip with an approach that does not assume index alignment. Either:

1. **Fail early**: assert `resolved_inputs.len() == transaction.outputs().len()` before the loop and return an error if they differ, or
2. **Match by type script group**: iterate over DAO-typed inputs and find their corresponding DAO-typed outputs by the group membership that the DAO script itself uses, rather than by array index. This is the robust fix and eliminates the positional assumption entirely.

---

### Proof of Concept

A transaction submitted via RPC or P2P with:
- `inputs  = [any_live_non_dao_cell, dao_deposit_cell_with_lock_size_S]`
- `outputs = [dao_withdraw_cell_with_lock_size_L (L≠S), any_non_dao_cell]`

passes `DaoScriptSizeVerifier::verify()` without triggering `TransactionError::DaoLockSizeMismatch`, because the zip produces pairs `(non_dao_input, dao_withdraw_output)` → skipped, and `(dao_deposit_input, non_dao_output)` → skipped. The deposit and withdraw cells are never compared, and the lock script size change goes undetected. [6](#0-5)

### Citations

**File:** verification/src/transaction_verifier.rs (L817-819)
```rust
/// Verifies that deposit cell and withdrawing cell in Nervos DAO use same sized lock scripts.
/// It provides a temporary solution till Nervos DAO script can be properly upgraded.
pub struct DaoScriptSizeVerifier<DL> {
```

**File:** verification/src/transaction_verifier.rs (L845-891)
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
}
```

**File:** tx-pool/src/util.rs (L111-114)
```rust
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L444-452)
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
```
