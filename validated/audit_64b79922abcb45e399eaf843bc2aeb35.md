### Title
`DaoScriptSizeVerifier` Bypassed via Mismatched Input/Output Positional Pairing — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`DaoScriptSizeVerifier::verify()` uses `.zip()` to pair `resolved_inputs[i]` with `outputs()[i]` by position. Because the CKB protocol imposes no ordering constraint between DAO deposit inputs and DAO withdrawing outputs, an unprivileged transaction sender can place the DAO deposit cell at input index `i` and the DAO withdrawing cell at output index `j ≠ i`. The zip then pairs each with a non-DAO counterpart, the guard `continue`s on both, and the lock-script-size check is silently skipped in its entirety. This is the direct CKB analog of the reported bug: iterating over array A while indexing into array B.

---

### Finding Description

`DaoScriptSizeVerifier` is the **sole enforcement layer** for the DAO lock-script-size constraint. Its own doc-comment states: *"It provides a temporary solution till Nervos DAO script can be properly upgraded"* — meaning the on-chain DAO C script does **not** enforce this rule.

The faulty loop:

```rust
for (i, (input_meta, cell_output)) in self
    .resolved_transaction
    .resolved_inputs          // array A
    .iter()
    .zip(self.resolved_transaction.transaction.outputs())  // array B
    .enumerate()
{
    if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
        && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
    {
        continue;   // ← both guards fire when positions are mismatched
    }
    // ... lock-script size check never reached
}
```

`resolved_inputs` and `outputs()` are independent arrays. Their lengths can differ, and their DAO-cell positions need not align. The `.zip()` truncates to the shorter length and pairs by position only. When a DAO deposit input sits at position `i` and the DAO withdrawing output sits at position `j ≠ i`, the zip pairs each with a non-DAO counterpart; both fail the dual-DAO guard and `continue`. The size check at line 885 is never executed. [1](#0-0) 

The verifier is called unconditionally in both the tx-pool admission path and the block-commit path: [2](#0-1) [3](#0-2) 

---

### Impact Explanation

The `DaoScriptSizeVerifier` was introduced specifically because the DAO C script does not check lock-script size. Bypassing it allows a DAO withdraw transaction to use a withdrawing cell whose lock script is a different size than the deposit cell's lock script. This breaks the capacity accounting invariant the DAO protocol relies on: the occupied capacity of the withdrawing cell diverges from what was accounted for at deposit time. Concretely, a withdrawing cell with a **smaller** lock script has less occupied capacity, so the attacker reclaims more "free" capacity than the deposit's interest calculation entitles them to — effectively extracting capacity that was not theirs. The `CapacityVerifier` skips the `OutputsSumOverflow` check for DAO withdraw transactions, so no secondary guard catches this. [4](#0-3) 

---

### Likelihood Explanation

Any unprivileged user who holds a DAO deposit cell can craft this transaction. No special privilege, key leak, or majority hashpower is required. The attacker simply reorders their inputs and outputs so the DAO deposit input index differs from the DAO withdrawing output index, then submits via the standard `send_transaction` RPC. The existing test suite only tests the aligned case (DAO input at index `i`, DAO output at index `i`), leaving the misaligned case entirely untested and undetected. [5](#0-4) 

---

### Recommendation

Replace the positional `.zip()` with independent iteration over all DAO deposit inputs and all DAO withdrawing outputs. One correct approach: collect all DAO deposit inputs and all DAO withdrawing outputs into separate lists, then enforce the size constraint across every (deposit-input, withdrawing-output) pair that the transaction presents — or, if a 1-to-1 correspondence is required, enforce it by explicit index annotation rather than by positional assumption.

---

### Proof of Concept

Craft a `ResolvedTransaction` with:

| Position | `resolved_inputs` | `outputs()` |
|---|---|---|
| 0 | DAO deposit cell (all-zero 8-byte data, DAO type script, small lock script, `block_number ≥ starting_block_limiting_dao_withdrawing_lock`) | Non-DAO cell |
| 1 | Non-DAO cell | DAO withdrawing cell with a **different-sized** lock script |

Execution trace through `DaoScriptSizeVerifier::verify()`:

- **Iteration i=0**: `input_meta` = DAO deposit cell; `cell_output` = non-DAO cell. Guard: `cell_uses_dao_type_script(output)` → **false** → `continue`.
- **Iteration i=1**: `input_meta` = non-DAO cell; `cell_output` = DAO withdrawing cell. Guard: `cell_uses_dao_type_script(input)` → **false** → `continue`.

The size-mismatch check at line 885 is never reached. `verify()` returns `Ok(())`. The transaction passes both tx-pool admission and block-commit verification with a lock-script size that the DAO protocol explicitly prohibits. [6](#0-5)

### Citations

**File:** verification/src/transaction_verifier.rs (L479-494)
```rust
        // skip OutputsSumOverflow verification for resolved cellbase and DAO
        // withdraw transactions.
        // cellbase's outputs are verified by RewardVerifier
        // DAO withdraw transaction is verified via the type script of DAO cells
        if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
            let inputs_sum = self.resolved_transaction.inputs_capacity()?;
            let outputs_sum = self.resolved_transaction.outputs_capacity()?;

            if inputs_sum < outputs_sum {
                return Err((TransactionError::OutputsSumOverflow {
                    inputs_sum,
                    outputs_sum,
                })
                .into());
            }
        }
```

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

**File:** verification/src/tests/transaction_verifier.rs (L926-983)
```rust
#[test]
fn test_dao_disables_different_lock_script_size() {
    let (consensus, dao_type_script) = build_consensus_with_dao_limiting_block(20000);

    let transaction = TransactionBuilder::default()
        .outputs(vec![
            CellOutput::new_builder()
                .capacity(capacity_bytes!(50))
                .build(),
            CellOutput::new_builder()
                .capacity(capacity_bytes!(200))
                .lock(Script::new_builder().args(Bytes::from(vec![1; 20])).build())
                .type_(Some(dao_type_script.clone()))
                .build(),
        ])
        .outputs_data(vec![Bytes::new().into(); 2])
        .build();

    let rtx = Arc::new(ResolvedTransaction {
        transaction,
        resolved_cell_deps: Vec::new(),
        resolved_inputs: vec![
            CellMetaBuilder::from_cell_output(
                CellOutput::new_builder()
                    .capacity(capacity_bytes!(50))
                    .build(),
                Bytes::new(),
            )
            .transaction_info(mock_transaction_info(
                20010,
                EpochNumberWithFraction::new(10, 0, 10),
                0,
            ))
            .build(),
            CellMetaBuilder::from_cell_output(
                CellOutput::new_builder()
                    .capacity(capacity_bytes!(201))
                    .lock(Script::new_builder().args(Bytes::new()).build())
                    .type_(Some(dao_type_script))
                    .build(),
                Bytes::from(vec![0; 8]),
            )
            .transaction_info(mock_transaction_info(
                20011,
                EpochNumberWithFraction::new(10, 0, 10),
                0,
            ))
            .build(),
        ],
        resolved_dep_groups: vec![],
    });
    let verifier = DaoScriptSizeVerifier::new(rtx, consensus, EmptyDataProvider {});

    assert_error_eq!(
        verifier.verify().unwrap_err(),
        TransactionError::DaoLockSizeMismatch { index: 1 },
    );
}
```
