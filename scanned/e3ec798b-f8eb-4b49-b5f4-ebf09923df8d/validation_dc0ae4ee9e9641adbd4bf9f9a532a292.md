### Title
`DaoScriptSizeVerifier` Bypasses Lock-Script-Size Constraint via Positional Index Mismatch, Enabling DAO Capacity Inflation — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`DaoScriptSizeVerifier::verify()` enforces that a DAO deposit cell and its corresponding DAO prepare cell (phase-1 output) carry lock scripts of identical byte size. The check is implemented by zipping `resolved_inputs` with `transaction.outputs()` positionally. Because CKB imposes no rule that a DAO deposit cell at input index `j` must correspond to the output at the same index `j`, a transaction author can place the deposit cell and the prepare cell at deliberately mismatched indices, causing the verifier to inspect only non-DAO pairs and silently pass. This bypasses the sole Rust-layer guard against lock-script-size changes between deposit and prepare, exposing the underlying DAO capacity-accounting vulnerability the verifier was introduced to patch.

---

### Finding Description

**Root cause — positional zip in `DaoScriptSizeVerifier::verify()`**

```
// verification/src/transaction_verifier.rs  L847-L888
for (i, (input_meta, cell_output)) in self
    .resolved_transaction
    .resolved_inputs
    .iter()
    .zip(self.resolved_transaction.transaction.outputs())   // ← positional pairing
    .enumerate()
{
    if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
        && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
    {
        continue;   // ← skipped unless BOTH sides at index i are DAO cells
    }
    ...
    if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
        return Err((TransactionError::DaoLockSizeMismatch { index: i }).into());
    }
}
```

The guard fires only when `resolved_inputs[i]` **and** `outputs[i]` both carry the DAO type script. There is no protocol rule requiring this alignment.

**Bypass construction**

Craft a phase-1 (deposit → prepare) transaction with:

| slot | cell | DAO type? | data |
|------|------|-----------|------|
| `inputs[0]` | ordinary cell (lock `L_dummy`) | no | — |
| `inputs[1]` | DAO deposit cell (lock `L_large`, 53 B, data = `[0u8;8]`) | yes | zeros |
| `outputs[0]` | DAO prepare cell (lock `L_small`, 33 B, data = block_number) | yes | block# |
| `outputs[1]` | ordinary cell | no | — |

The zip produces two pairs:
- `(inputs[0], outputs[0])` → `inputs[0]` is not DAO → `continue`
- `(inputs[1], outputs[1])` → `outputs[1]` is not DAO → `continue`

The actual DAO deposit→prepare pair `(inputs[1], outputs[0])` is **never evaluated**. The verifier returns `Ok(())`.

**Downstream capacity inflation**

`DaoCalculator::calculate_maximum_withdraw()` computes the withdrawal ceiling from the **prepare cell's** lock script size:

```
// util/dao/src/lib.rs  L149-L156
let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
let counted_capacity  = output_capacity.safe_sub(occupied_capacity)?;
let withdraw_counted_capacity =
    u128::from(counted_capacity.as_u64()) * u128::from(withdrawing_ar) / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

Shrinking the lock script from 53 B to 33 B reduces `occupied_capacity` by 20 bytes (2 000 shannons), increases `counted_capacity` by the same amount, and scales the interest-bearing portion upward by `withdrawing_ar / deposit_ar`. The attacker withdraws more CKB than they deposited; the surplus is drawn from the DAO secondary-issuance pool shared by all depositors.

**Enforcement points**

The verifier is called at both the tx-pool admission layer (unconditionally) and the block-verification layer (gated on `rfc0044_active`):

```
// tx-pool/src/util.rs  L111-L113
DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
    .verify()?;

// verification/contextual/src/contextual_block_verifier.rs  L445-L451
if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
    DaoScriptSizeVerifier::new(...).verify()?;
}
```

Both call sites share the same flawed positional-zip logic.

---

### Impact Explanation

An unprivileged DAO depositor can change the lock script of their own DAO cell to a smaller one during phase 1 without triggering `DaoLockSizeMismatch`. In phase 2 the `DaoCalculator` (and the on-chain DAO type script, which uses the same prepare-cell geometry) computes a higher maximum withdrawal than the depositor is entitled to. The excess CKB is drawn from the secondary-issuance pool, constituting a direct, repeatable theft from other DAO participants. The magnitude per attack is proportional to the lock-script-size reduction and the DAO interest rate; it is small per transaction but unbounded in aggregate across many deposits.

---

### Likelihood Explanation

Any CKB holder who has deposited into the DAO can execute this attack with a single crafted phase-1 transaction. No privileged keys, no majority hashpower, and no cooperation from other parties are required. The transaction structure is valid by all other consensus rules. The attack is deterministic and reproducible on mainnet after `rfc0044` activation.

---

### Recommendation

Replace the positional `.zip()` with an explicit cross-product check that identifies every DAO deposit cell among the inputs and every DAO prepare cell among the outputs independently, then verifies the lock-script-size constraint for each such deposit cell regardless of its index. Concretely:

1. Collect all `(index, input_meta)` pairs where the input is a DAO deposit cell (DAO type script + all-zero data + block ≥ `starting_block_limiting_dao_withdrawing_lock`).
2. Collect all `(index, cell_output)` pairs where the output is a DAO prepare cell (DAO type script).
3. For each deposit cell, assert that **every** DAO prepare cell in the same transaction carries a lock script of the same byte size, or enforce a 1-to-1 correspondence through an explicit protocol rule (e.g., require that the deposit cell at input `i` maps to the prepare cell at output `i` and reject transactions that violate this mapping).

---

### Proof of Concept

```
// Phase-1 transaction layout that bypasses DaoScriptSizeVerifier
inputs:
  [0] ordinary cell          (lock = L_dummy, no DAO type)
  [1] DAO deposit cell       (lock = L_large [53 B], type = DAO, data = [0u8;8])

outputs:
  [0] DAO prepare cell       (lock = L_small [33 B], type = DAO, data = block_number_le)
  [1] ordinary cell          (no DAO type)

// DaoScriptSizeVerifier iterates:
//   i=0: inputs[0] not DAO  → continue
//   i=1: outputs[1] not DAO → continue
// → verify() returns Ok(())   ← check fully bypassed

// Phase-2 withdrawal:
//   DaoCalculator uses L_small (33 B) for occupied_capacity
//   counted_capacity is 20 bytes (2 000 shannons) larger than it should be
//   withdraw_capacity > legitimate entitlement
//   Surplus drawn from DAO secondary-issuance pool
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

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

**File:** tx-pool/src/util.rs (L111-113)
```rust
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
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
