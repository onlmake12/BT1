Audit Report

## Title
`DaoScriptSizeVerifier::verify()` Positional-Zip Index Mismatch Allows Lock-Script-Size Change in DAO Phase-1, Enabling Capacity Inflation — (`File: verification/src/transaction_verifier.rs`)

## Summary

`DaoScriptSizeVerifier::verify()` pairs `resolved_inputs` with `transaction.outputs()` using a positional `.zip()`, so the lock-script-size check only fires when a DAO deposit cell and a DAO prepare cell appear at the **same absolute index**. An attacker can place the deposit cell at input `j` and the prepare cell at output `k ≠ j`, causing the verifier to inspect only non-DAO pairs and return `Ok(())`. The on-chain DAO type script does not check lock-script size (it only checks capacity equality within its script group), so the smaller-lock prepare cell is committed to chain. In phase 2, `DaoCalculator::calculate_maximum_withdraw()` computes a higher withdrawal ceiling from the prepare cell's reduced `occupied_capacity`, allowing the attacker to extract more CKB than they deposited.

## Finding Description

**Root cause — positional zip in `DaoScriptSizeVerifier::verify()`**

The loop at `verification/src/transaction_verifier.rs` L847–851 zips `resolved_inputs` with `transaction.outputs()` by absolute position:

```rust
for (i, (input_meta, cell_output)) in self
    .resolved_transaction
    .resolved_inputs
    .iter()
    .zip(self.resolved_transaction.transaction.outputs())  // positional
    .enumerate()
{
    if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
        && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
    {
        continue;   // skipped unless BOTH sides at index i are DAO cells
    }
    ...
    if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
        return Err(...DaoLockSizeMismatch { index: i }...);
    }
}
```

The error message itself encodes the flawed assumption: `"The lock script size of deposit cell at index {} does not match the withdrawing cell at the same index"`. No protocol rule requires a DAO deposit cell at input `j` to correspond to the prepare cell at output `j`.

**Bypass construction**

Craft a phase-1 transaction:

| slot | cell | DAO type? |
|------|------|-----------|
| `inputs[0]` | ordinary cell (lock `L_dummy`) | no |
| `inputs[1]` | DAO deposit cell (lock `L_large`, data = `[0u8;8]`) | yes |
| `outputs[0]` | DAO prepare cell (lock `L_small`, data = block_number) | yes |
| `outputs[1]` | ordinary cell | no |

The zip produces:
- `(inputs[0], outputs[0])` → `inputs[0]` not DAO → `continue`
- `(inputs[1], outputs[1])` → `outputs[1]` not DAO → `continue`

The actual DAO pair `(inputs[1], outputs[0])` is never evaluated. The verifier returns `Ok(())`.

**Why the DAO type script does not block this**

The on-chain DAO type script runs within its script group. In the bypass layout, the group contains `group_input[0] = inputs[1]` and `group_output[0] = outputs[0]`. The DAO type script checks that `group_output[0].capacity == deposit_capacity` (capacity equality), but does **not** check lock-script size — that is precisely why `DaoScriptSizeVerifier` was introduced as a "temporary solution till Nervos DAO script can be properly upgraded." The capacity check passes because the attacker preserves the cell capacity.

**Downstream capacity inflation**

`DaoCalculator::calculate_maximum_withdraw()` at `util/dao/src/lib.rs` L149–156 computes:

```rust
let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
let counted_capacity  = output_capacity.safe_sub(occupied_capacity)?;
let withdraw_counted_capacity =
    u128::from(counted_capacity.as_u64()) * u128::from(withdrawing_ar) / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

`output` here is the **prepare cell**. Shrinking the lock script by `Δ` bytes reduces `occupied_capacity` by `100 * Δ` shannons, increases `counted_capacity` by the same amount, and scales the interest-bearing portion upward by `withdrawing_ar / deposit_ar`. The attacker withdraws more than they deposited; the surplus is drawn from the DAO secondary-issuance pool.

**Both enforcement points share the same flaw**

- tx-pool: `tx-pool/src/util.rs` L111–113
- block verification: `verification/contextual/src/contextual_block_verifier.rs` L445–451

Both call the same `DaoScriptSizeVerifier::verify()` with the same positional-zip logic.

## Impact Explanation

This is a **Critical** vulnerability matching the allowed impact class: *"Vulnerabilities which could easily damage CKB economy."* An attacker can repeatably extract CKB from the DAO secondary-issuance pool — funds shared by all depositors — without any privileged access. The surplus per attack is proportional to the lock-script-size reduction and the DAO interest rate; it is unbounded in aggregate across many deposits and many epochs.

## Likelihood Explanation

Any CKB holder who has deposited into the DAO can execute this attack. The required transaction structure is valid under all other consensus rules. No privileged keys, no majority hashpower, and no cooperation from other parties are required. The attack is deterministic and reproducible on mainnet after `rfc0044` activation (the block-verification gate) and immediately at the tx-pool layer (which applies the verifier unconditionally). The attack is repeatable with each new deposit.

## Recommendation

Replace the positional `.zip()` with an index-independent cross-check:

1. Collect all `(j, input_meta)` pairs where the input is a DAO deposit cell (DAO type script + all-zero 8-byte data + block ≥ `starting_block_limiting_dao_withdrawing_lock`).
2. Collect all `(k, cell_output)` pairs where the output is a DAO prepare cell (DAO type script).
3. Determine the deposit→prepare correspondence using the same group-index logic the DAO type script uses (group input `i` maps to group output `i`), or enforce a protocol rule that the deposit cell at absolute input `j` must map to the prepare cell at absolute output `j` and reject transactions that violate this mapping.
4. For each identified deposit→prepare pair, assert `deposit_lock.total_size() == prepare_lock.total_size()`.

Additionally, add a unit test that places the DAO deposit at input index 1 and the DAO prepare at output index 0 (misaligned) with differing lock-script sizes, and asserts that `verify()` returns `Err(DaoLockSizeMismatch)`.

## Proof of Concept

```
// Phase-1 transaction layout that bypasses DaoScriptSizeVerifier
inputs:
  [0] ordinary cell          (lock = L_dummy, no DAO type)
  [1] DAO deposit cell       (lock = L_large, type = DAO, data = [0u8;8],
                              block_number >= starting_block_limiting_dao_withdrawing_lock)

outputs:
  [0] DAO prepare cell       (lock = L_small, type = DAO, data = block_number_le)
  [1] ordinary cell          (no DAO type)

// DaoScriptSizeVerifier iterates:
//   i=0: inputs[0] not DAO  → continue
//   i=1: outputs[1] not DAO → continue
// → verify() returns Ok(())   ← check fully bypassed

// DAO type script (group context):
//   group_input[0]  = inputs[1]  (deposit, L_large)
//   group_output[0] = outputs[0] (prepare, L_small)
//   capacity check: outputs[0].capacity == deposit_capacity → passes
//   lock-script size: not checked by DAO type script → passes

// Phase-2 withdrawal:
//   DaoCalculator uses L_small for occupied_capacity
//   counted_capacity is (L_large_size - L_small_size) * 100 shannons larger
//   withdraw_capacity > legitimate entitlement
//   Surplus drawn from DAO secondary-issuance pool
```

The bypass can be confirmed with a unit test mirroring `test_dao_disables_different_lock_script_size` (at `verification/src/tests/transaction_verifier.rs` L927) but with the DAO deposit at `resolved_inputs[1]` and the DAO prepare at `outputs[0]` — the existing test only covers the aligned case (`inputs[1]` / `outputs[1]`) and would pass, while the misaligned case would incorrectly return `Ok(())` with the current code. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** verification/src/transaction_verifier.rs (L847-858)
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
```

**File:** verification/src/transaction_verifier.rs (L883-887)
```rust
            // Now we have a pair of DAO deposit and withdrawing cells, it is expected
            // they have the lock scripts of the same size.
            if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
                return Err((TransactionError::DaoLockSizeMismatch { index: i }).into());
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

**File:** util/types/src/core/error.rs (L203-211)
```rust
    /// Nervos DAO lock size mismatch.
    #[error(
        "The lock script size of deposit cell at index {} does not match the withdrawing cell at the same index",
        index
    )]
    DaoLockSizeMismatch {
        /// The index of mismatched DAO cells.
        index: usize,
    },
```

**File:** verification/src/tests/transaction_verifier.rs (L927-983)
```rust
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
