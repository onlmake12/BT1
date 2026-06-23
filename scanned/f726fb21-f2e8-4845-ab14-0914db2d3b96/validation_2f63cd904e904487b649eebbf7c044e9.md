### Title
`DaoScriptSizeVerifier` Lock-Script-Size Check Bypassed via Index Misalignment — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`DaoScriptSizeVerifier::verify()` enforces RFC 0044: when a DAO deposit cell is spent and a DAO withdrawal cell is created in the same transaction, their lock scripts must be the same byte size. The verifier enforces this by zipping `resolved_inputs[i]` with `outputs[i]` and only checking the pair when **both** slots carry a DAO type script. Because there is no protocol rule requiring the DAO input and the DAO output to occupy the same index, an unprivileged transaction sender can trivially place them at different indices, causing every pair to fail the "both are DAO" guard and the size check to be silently skipped entirely.

---

### Finding Description

**Root cause — `verification/src/transaction_verifier.rs`, lines 847–888:**

```rust
for (i, (input_meta, cell_output)) in self
    .resolved_transaction
    .resolved_inputs
    .iter()
    .zip(self.resolved_transaction.transaction.outputs())   // ← pairs by position
    .enumerate()
{
    // Both the input and output cell must use Nervos DAO as type script
    if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
        && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
    {
        continue;   // ← silently skips the pair
    }
    ...
    if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
        return Err((TransactionError::DaoLockSizeMismatch { index: i }).into());
    }
}
```

The verifier assumes that the DAO deposit input and the DAO withdrawal output share the same transaction index. CKB imposes no such constraint. A sender who places them at **different** indices produces only "mixed" pairs — each pair has exactly one DAO cell — so every iteration hits `continue` and the function returns `Ok(())` without ever reaching the size comparison.

**Concrete bypass layout:**

| Slot | Role | DAO? |
|---|---|---|
| `inputs[0]` | fee / non-DAO cell | No |
| `inputs[1]` | DAO deposit cell (large lock, e.g. 200 B) | Yes |
| `outputs[0]` | DAO withdrawal cell (small lock, e.g. 20 B) | Yes |
| `outputs[1]` | change cell | No |

Pairs evaluated by `.zip()`:
- `(inputs[0], outputs[0])` → inputs[0] not DAO → `continue`
- `(inputs[1], outputs[1])` → outputs[1] not DAO → `continue`

Result: `verify()` returns `Ok(())`. The lock-script-size mismatch is never detected.

**Where `DaoScriptSizeVerifier` is invoked:**

It is called unconditionally (when `rfc0044_active`) in both the block-level verifier and the tx-pool admission path, so the bypass works in both contexts. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

RFC 0044 / `DaoScriptSizeVerifier` exists specifically to prevent lock-script substitution during DAO withdrawal — described in the code as "a temporary solution till Nervos DAO script can be properly upgraded." Bypassing it allows a depositor to:

1. Deposit with a large multisig lock script (high occupied capacity, high minimum-capacity requirement).
2. Withdraw into a cell with a much smaller lock script, reducing the minimum occupied capacity of the output cell while keeping the full DAO-calculated payout (which is based on the deposit cell's total capacity, not its occupied capacity).
3. The freed capacity headroom can be used to under-fund the output cell's occupied capacity in ways the DAO on-chain script does not independently check, or to violate the invariant that the RFC was designed to close.

More broadly, this is a **consensus rule bypass**: nodes that apply the check correctly and nodes that receive a crafted transaction will disagree on validity, which is a chain-split / finality risk if the bypass is exploited at scale. [3](#0-2) [4](#0-3) 

---

### Likelihood Explanation

- **No privilege required.** Any CKB transaction sender can craft the index-misaligned layout.
- **Trivially constructable.** Adding a single non-DAO input before the DAO deposit input (e.g., a fee cell) is standard practice and sufficient to trigger the bypass.
- **Affects all DAO deposits committed after `starting_block_limiting_dao_withdrawing_lock`.** The check is gated on that block number, so older deposits are unaffected, but all new deposits are in scope.
- **No on-chain DAO script defence.** The DAO script verifies the interest arithmetic but does not enforce lock-script size equality; that is entirely the responsibility of the host-node verifier. [5](#0-4) 

---

### Recommendation

Replace the index-coupled `.zip()` loop with a two-pass approach that independently collects all DAO deposit inputs and all DAO withdrawal outputs, then matches them by the DAO-script-defined pairing (e.g., by the deposit block number stored in the withdrawal cell's data, or simply by enforcing that every DAO deposit input has a corresponding DAO withdrawal output of the same lock size regardless of index):

```rust
// Collect all DAO deposit inputs (data == all zeros, committed after threshold)
let dao_deposit_lock_sizes: Vec<usize> = self.resolved_transaction.resolved_inputs
    .iter()
    .filter(|meta| cell_uses_dao_type_script(&meta.cell_output, &dao_type_hash))
    .filter(|meta| /* data all zeros and block_number >= threshold */)
    .map(|meta| meta.cell_output.lock().total_size())
    .collect();

// Collect all DAO withdrawal outputs
let dao_withdrawal_lock_sizes: Vec<usize> = self.resolved_transaction.transaction.outputs()
    .into_iter()
    .filter(|output| cell_uses_dao_type_script(output, &dao_type_hash))
    .map(|output| output.lock().total_size())
    .collect();

// Enforce size equality for each matched pair
for (deposit_size, withdrawal_size) in dao_deposit_lock_sizes.iter().zip(dao_withdrawal_lock_sizes.iter()) {
    if deposit_size != withdrawal_size {
        return Err(...);
    }
}
```

The exact matching strategy should follow the DAO protocol's pairing semantics (one deposit input → one withdrawal output), but any strategy is strictly better than the current index-coupled check. [6](#0-5) 

---

### Proof of Concept

**Transaction structure:**

```
inputs:
  [0]  OutPoint(fee_cell)          lock=secp256k1(20B)   type=None       data=0x
  [1]  OutPoint(dao_deposit_cell)  lock=multisig(200B)   type=DAO        data=0x0000000000000000

outputs:
  [0]  capacity=C+interest         lock=secp256k1(20B)   type=DAO        data=<deposit_block_number>
  [1]  capacity=change             lock=secp256k1(20B)   type=None       data=0x

witnesses: [fee_witness, dao_witness]
```

**Verifier trace:**

1. `zip` produces pairs: `(inputs[0], outputs[0])` and `(inputs[1], outputs[1])`.
2. Pair 0: `inputs[0]` has no DAO type → `continue`.
3. Pair 1: `outputs[1]` has no DAO type → `continue`.
4. Loop ends. `verify()` returns `Ok(())`.
5. The lock script changed from 200 B (multisig) to 20 B (secp256k1) with no error.

**Expected (correct) behaviour:** `DaoLockSizeMismatch { index: 0 }` error, blocking the transaction. [6](#0-5) [4](#0-3)

### Citations

**File:** verification/src/transaction_verifier.rs (L817-819)
```rust
/// Verifies that deposit cell and withdrawing cell in Nervos DAO use same sized lock scripts.
/// It provides a temporary solution till Nervos DAO script can be properly upgraded.
pub struct DaoScriptSizeVerifier<DL> {
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
