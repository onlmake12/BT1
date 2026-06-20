### Title
`DaoScriptSizeVerifier` Index-Based Pairing Allows Lock Script Size Change Between DAO Deposit and Prepare — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`DaoScriptSizeVerifier::verify()` enforces that a DAO deposit cell and its corresponding prepare cell use lock scripts of the same byte size. However, the pairing is done strictly by position using `zip` over `resolved_inputs` and `transaction.outputs()`. An unprivileged transaction sender can place the DAO deposit cell at input index `i` and the DAO prepare cell at output index `j ≠ i`, causing the verifier to never compare the two cells. The DAO type script (C code) does not check lock script size, so the bypass is complete. In phase 2 (withdraw), the interest calculation uses the prepare cell's occupied capacity, which is now based on the smaller lock script, yielding more interest than the depositor is entitled to.

---

### Finding Description

`DaoScriptSizeVerifier::verify()` iterates over `(resolved_inputs, outputs)` pairs using `zip`:

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
    // Only input data with full zeros are counted as deposit cell
    if input_data.into_iter().any(|b| b != 0) { continue; }
    // ...
    if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
        return Err(TransactionError::DaoLockSizeMismatch { index: i }.into());
    }
}
```

The check only fires when **both** the input at index `i` and the output at index `i` carry the DAO type script. A transaction structured as:

| Slot | Input | Output |
|------|-------|--------|
| 0 | non-DAO cell | DAO prepare cell (small lock) |
| 1 | DAO deposit cell (large lock) | non-DAO cell |

causes the verifier to evaluate:
- Pair (0): non-DAO input → `cell_uses_dao_type_script` fails → `continue`
- Pair (1): non-DAO output → `cell_uses_dao_type_script` fails → `continue`

No lock-size comparison is ever performed. The DAO type script (C) only verifies that output capacity equals input capacity in phase 1; it does not verify lock script size. The `DaoScriptSizeVerifier` comment explicitly acknowledges this:

> *"It provides a temporary solution till Nervos DAO script can be properly upgraded."* [1](#0-0) 

---

### Impact Explanation

In phase 2 (withdraw), `DaoCalculator::calculate_maximum_withdraw` computes interest as:

```rust
let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
```

`output` here is the phase-2 input (the prepare cell). If the prepare cell's lock script is smaller than the deposit cell's lock script, `occupied_capacity` is smaller, `counted_capacity` (the "free" capacity that earns interest) is larger, and the attacker receives more interest than they deposited. The excess interest is drawn from the DAO secondary issuance pool, reducing the share available to honest depositors. The discrepancy is proportional to the lock script size reduction multiplied by the DAO accumulation ratio growth. [2](#0-1) 

---

### Likelihood Explanation

Any unprivileged transaction sender who has previously deposited into the DAO can craft this transaction. No special privilege, key leak, or majority hashpower is required. The attacker only needs to:
1. Own a DAO deposit cell with a large lock script (e.g., a multisig or custom lock with long `args`).
2. Submit a phase-1 prepare transaction with the deposit cell at input index ≥ 1 and the prepare cell (with a minimal lock script) at output index 0.

The `DaoScriptSizeVerifier` is invoked in both the tx-pool admission path and the block verifier path, but both use the same flawed `zip`-based pairing, so neither catches the bypass. [3](#0-2) [4](#0-3) 

---

### Recommendation

Replace the index-based `zip` pairing with an explicit search that matches each DAO deposit input against the DAO output that the DAO type script itself would pair with it (i.e., the output at the same index as the input, or alternatively, scan all outputs for a DAO cell and verify every such pair regardless of positional alignment). At minimum, the loop should be restructured so that a DAO deposit cell at input index `i` is checked against the output at the same index `i`, and if no DAO output exists at that index, the transaction should be rejected rather than silently skipped.

Additionally, add a test case that places the DAO deposit cell at input index 1 with a large lock script and the DAO prepare cell at output index 0 with a small lock script, and asserts that `DaoScriptSizeVerifier::verify()` returns `Err(DaoLockSizeMismatch)`.

---

### Proof of Concept

Construct a `ResolvedTransaction` with:

```
resolved_inputs[0] = non-DAO cell, any lock, data = empty
resolved_inputs[1] = DAO deposit cell, lock.args = vec![0u8; 100], data = [0u8; 8]
                     transaction_info.block_number >= starting_block_limiting_dao_withdrawing_lock

outputs[0] = DAO prepare cell, lock.args = vec![], same capacity as deposit cell
outputs[1] = non-DAO cell
```

Call `DaoScriptSizeVerifier::new(rtx, consensus, data_loader).verify()`.

- Pair (0): `resolved_inputs[0]` is non-DAO → `cell_uses_dao_type_script` returns false → `continue`.
- Pair (1): `outputs[1]` is non-DAO → `cell_uses_dao_type_script` returns false → `continue`.

Result: `Ok(())` — the lock script size change from 133 bytes to 33 bytes is never detected.

In phase 2, `calculate_maximum_withdraw` uses the prepare cell's `occupied_capacity` (33 bytes lock), so `counted_capacity` is 100 bytes larger than it should be, and the attacker earns interest on that phantom 100 bytes of "free" capacity. [5](#0-4) [6](#0-5)

### Citations

**File:** verification/src/transaction_verifier.rs (L843-890)
```rust
    /// Verifies that for all Nervos DAO transactions, withdrawing cells must use lock scripts
    /// of the same size as corresponding deposit cells
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

**File:** util/dao/src/lib.rs (L149-158)
```rust
        let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
        let output_capacity: Capacity = output.capacity().into();
        let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

        Ok(withdraw_capacity)
```

**File:** tx-pool/src/util.rs (L111-114)
```rust
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
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
