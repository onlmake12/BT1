Audit Report

## Title
`DaoScriptSizeVerifier` Lock-Script-Size Check Bypassed via Index Misalignment — (File: verification/src/transaction_verifier.rs)

## Summary

`DaoScriptSizeVerifier::verify()` enforces RFC 0044 by requiring that a DAO deposit input and its corresponding DAO withdrawal output carry lock scripts of equal byte size. The check is implemented by zipping `resolved_inputs` with `outputs` positionally, so it only fires when both slots at the same index carry a DAO type script. Because CKB imposes no protocol rule requiring the DAO input and DAO output to occupy the same index, any unprivileged sender can place them at different indices, causing every pair to fail the "both are DAO" guard and the size check to be silently skipped entirely.

## Finding Description

The root cause is in `verify()` at lines 847–852:

```rust
for (i, (input_meta, cell_output)) in self
    .resolved_transaction
    .resolved_inputs
    .iter()
    .zip(self.resolved_transaction.transaction.outputs())  // positional pairing
    .enumerate()
``` [1](#0-0) 

The guard at lines 855–858 skips any pair where either slot is not a DAO cell:

```rust
if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
    && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
{
    continue;
}
``` [2](#0-1) 

The size comparison at line 885 is only reached if both cells in the same positional slot are DAO cells: [3](#0-2) 

**Bypass layout:**

| Slot | Cell | DAO? |
|---|---|---|
| `inputs[0]` | fee cell | No |
| `inputs[1]` | DAO deposit (200 B lock) | Yes |
| `outputs[0]` | DAO withdrawal (20 B lock) | Yes |
| `outputs[1]` | change cell | No |

`.zip()` produces pairs `(inputs[0], outputs[0])` and `(inputs[1], outputs[1])`. Pair 0: `inputs[0]` is not DAO → `continue`. Pair 1: `outputs[1]` is not DAO → `continue`. The loop exits and `verify()` returns `Ok(())` without ever reaching the size comparison. The lock script changed from 200 B to 20 B with no error.

The verifier is invoked unconditionally (when `rfc0044_active`) in both the block-level verifier and the tx-pool admission path, so the bypass is effective in both contexts: [4](#0-3) [5](#0-4) 

## Impact Explanation

This is a **consensus rule bypass** matching the Critical impact class: "Vulnerabilities which could easily cause consensus deviation." Nodes that correctly apply the RFC 0044 check and nodes that receive a crafted index-misaligned transaction will disagree on its validity. If exploited at scale, this produces a chain split. Additionally, bypassing the lock-script-size check allows a depositor to substitute a smaller lock script during withdrawal, violating the invariant RFC 0044 was designed to close — a direct economic impact on the DAO mechanism.

## Likelihood Explanation

No privilege is required. Any CKB transaction sender can craft the bypass by placing a single non-DAO input (e.g., a standard fee cell) before the DAO deposit input — a pattern that is standard practice. The bypass is deterministic and repeatable. It affects all DAO deposits committed after `starting_block_limiting_dao_withdrawing_lock`. [6](#0-5) 

## Recommendation

Replace the positional `.zip()` loop with a two-pass approach: independently collect all DAO deposit inputs (data all-zeros, block number ≥ threshold) and all DAO withdrawal outputs (DAO type script), then enforce lock-script size equality for each matched pair regardless of index. The matching strategy should follow the DAO protocol's pairing semantics (one deposit input → one withdrawal output), but any index-independent strategy is strictly correct where the current one is not.

## Proof of Concept

Construct a transaction:

```
inputs:
  [0]  fee_cell          lock=secp256k1(20B)  type=None  data=0x
  [1]  dao_deposit_cell  lock=multisig(200B)  type=DAO   data=0x0000000000000000

outputs:
  [0]  capacity=C+interest  lock=secp256k1(20B)  type=DAO  data=<deposit_block_number>
  [1]  change_cell          lock=secp256k1(20B)  type=None data=0x
```

Submit to a node with `rfc0044_active`. Trace through `DaoScriptSizeVerifier::verify()`:

1. Pair `(inputs[0], outputs[0])`: `inputs[0]` has no DAO type → `continue`.
2. Pair `(inputs[1], outputs[1])`: `outputs[1]` has no DAO type → `continue`.
3. Loop ends. Returns `Ok(())`.

Expected correct behavior: `DaoLockSizeMismatch { index: 0 }` error blocking the transaction. [7](#0-6)

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

**File:** tx-pool/src/util.rs (L110-114)
```rust
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
```
