### Title
Positional Index Assumption in `DaoScriptSizeVerifier` Allows Lock Script Size Mismatch Bypass — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`DaoScriptSizeVerifier::verify()` pairs inputs with outputs strictly by position using `.zip()`. A transaction sender can place a DAO deposit input at index `j` and the corresponding DAO withdrawing output at index `k` (where `j ≠ k`), causing the lock-script-size enforcement to be silently skipped. This bypasses RFC-0044's protection and allows a user to earn inflated DAO interest by using a smaller lock script in the withdrawing cell than in the deposit cell.

---

### Finding Description

`DaoScriptSizeVerifier::verify()` iterates over `(resolved_inputs[i], outputs[i])` pairs using `.zip()`: [1](#0-0) 

For each pair it checks:
1. Both `input[i]` and `output[i]` carry the DAO type script.
2. `input[i]` data is all-zero bytes (i.e., it is a deposit cell, not a withdrawing cell).
3. The lock script sizes match. [2](#0-1) 

The flaw is the implicit assumption that the DAO deposit input at position `i` always corresponds to the DAO withdrawing output at position `i`. A transaction author controls the ordering of inputs and outputs. By placing a non-DAO cell at input index 0 and the DAO deposit cell at input index 1, while placing the DAO withdrawing output at output index 0 and a change cell at output index 1, the zip produces:

| Pair | Input | Output | Both DAO? | Action |
|------|-------|--------|-----------|--------|
| 0 | regular cell | DAO withdrawing cell | No | `continue` (skip) |
| 1 | DAO deposit cell | change cell | No | `continue` (skip) |

The lock-script-size check is never reached. The verifier returns `Ok(())`.

The on-chain DAO type script does not enforce lock script size equality — that is precisely why `DaoScriptSizeVerifier` was introduced as a "temporary solution": [3](#0-2) 

The verifier is invoked in both the tx-pool admission path and the block verification path: [4](#0-3) [5](#0-4) 

---

### Impact Explanation

When the prepare (phase-1 withdrawal) transaction is accepted with a smaller lock script in the withdrawing cell, the phase-2 withdrawal's interest calculation in `calculate_maximum_withdraw` uses the withdrawing cell's occupied capacity: [6](#0-5) 

A smaller lock script → smaller `occupied_capacity` → larger `counted_capacity` → larger `withdraw_counted_capacity` → inflated withdrawal amount. The attacker extracts more CKB than the protocol intends to issue as DAO interest, at the expense of the DAO secondary issuance pool. The `DaoCalculator.withdrawed_interests()` used to update the DAO field will also be inflated, corrupting the on-chain `S` (secondary issuance surplus) accounting: [7](#0-6) 

---

### Likelihood Explanation

Any unprivileged transaction sender can exploit this. No special keys, hashpower, or coordination is required. The attacker only needs to:
- Hold a DAO deposit cell.
- Construct a prepare transaction with a non-DAO input at index 0 and the DAO deposit cell at index 1, while placing the DAO withdrawing output (with a smaller lock script) at index 0.

This is a standard transaction construction operation accessible via the RPC (`send_transaction`) or any CKB SDK. The bypass is deterministic and requires no brute force.

---

### Recommendation

Replace the positional `.zip()` with independent iteration over all inputs and all outputs. For each DAO deposit input (all-zero data, committed after `starting_block_limiting_dao_withdrawing_lock`), search all outputs for a DAO withdrawing output and enforce that the lock script sizes match. The check must not rely on index alignment between inputs and outputs.

---

### Proof of Concept

**Step 1 — Deposit**: Create a DAO deposit cell with a secp256k1 lock script (e.g., 53-byte total lock size), capacity = 1,000,000 CKB, data = `[0u8; 8]`.

**Step 2 — Prepare (exploit)**: Construct a prepare transaction:

```
inputs:
  [0] = regular non-DAO cell (e.g., 100 CKB)
  [1] = DAO deposit cell (1,000,000 CKB, lock size 53 bytes, data = [0u8;8])

outputs:
  [0] = DAO withdrawing cell (1,000,000 CKB, lock size 33 bytes, data = deposit_block_number_le)
  [1] = change cell (100 CKB, non-DAO)
```

`DaoScriptSizeVerifier` evaluates:
- Pair (input[0]=regular, output[0]=DAO withdrawing): `cell_uses_dao_type_script(regular)` = false → `continue`
- Pair (input[1]=DAO deposit, output[1]=change): `cell_uses_dao_type_script(change)` = false → `continue`

Verifier returns `Ok(())`. The prepare transaction is accepted into the tx-pool and committed to a block. [8](#0-7) 

**Step 3 — Withdraw**: Submit the phase-2 withdrawal. `calculate_maximum_withdraw` computes interest using `occupied_capacity` of the 33-byte-lock withdrawing cell, which is ~2,000 shannons smaller than the 53-byte-lock deposit cell's occupied capacity. The attacker receives proportionally more interest than the protocol intends, and the DAO `S` field is decremented by the inflated amount, permanently corrupting the secondary issuance accounting. [9](#0-8)

### Citations

**File:** verification/src/transaction_verifier.rs (L817-818)
```rust
/// Verifies that deposit cell and withdrawing cell in Nervos DAO use same sized lock scripts.
/// It provides a temporary solution till Nervos DAO script can be properly upgraded.
```

**File:** verification/src/transaction_verifier.rs (L847-852)
```rust
        for (i, (input_meta, cell_output)) in self
            .resolved_transaction
            .resolved_inputs
            .iter()
            .zip(self.resolved_transaction.transaction.outputs())
            .enumerate()
```

**File:** verification/src/transaction_verifier.rs (L854-886)
```rust
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

**File:** util/dao/src/lib.rs (L312-333)
```rust
    fn withdrawed_interests(
        &self,
        mut rtxs: impl Iterator<Item = &'a ResolvedTransaction> + Clone,
    ) -> Result<Capacity, DaoError> {
        let maximum_withdraws = rtxs.clone().try_fold(Capacity::zero(), |capacities, rtx| {
            self.transaction_maximum_withdraw(rtx)
                .and_then(|c| capacities.safe_add(c).map_err(Into::into))
        })?;
        let input_capacities = rtxs.try_fold(Capacity::zero(), |capacities, rtx| {
            let tx_input_capacities = rtx.resolved_inputs.iter().try_fold(
                Capacity::zero(),
                |tx_capacities, cell_meta| {
                    let output_capacity: Capacity = cell_meta.cell_output.capacity().into();
                    tx_capacities.safe_add(output_capacity)
                },
            )?;
            capacities.safe_add(tx_input_capacities)
        })?;
        maximum_withdraws
            .safe_sub(input_capacities)
            .map_err(Into::into)
    }
```
