### Title
`MaturityVerifier` Does Not Check `resolved_dep_groups` for Cellbase Immaturity — (File: `verification/src/transaction_verifier.rs`)

### Summary
`MaturityVerifier::verify()` checks `resolved_inputs` and `resolved_cell_deps` for cellbase immaturity but silently omits `resolved_dep_groups`. A transaction sender can reference an immature cellbase output as a dep-group cell, bypassing the maturity consensus rule entirely.

### Finding Description
`ResolvedTransaction` carries three distinct cell collections:

- `resolved_inputs` — cells being spent
- `resolved_cell_deps` — code/data cells expanded from `DepType::Code` deps and from the inner cells of dep-groups
- `resolved_dep_groups` — the dep-group container cells themselves (one per `DepType::DepGroup` dep) [1](#0-0) 

When `resolve_transaction_dep` processes a `DepType::DepGroup` entry, it resolves the container cell, parses its data as a list of out-points, pushes the inner cells into `resolved_cell_deps`, and pushes the container cell into `resolved_dep_groups`. [2](#0-1) 

`MaturityVerifier::verify()` then iterates over `resolved_inputs` and `resolved_cell_deps` to enforce the cellbase maturity rule, but never touches `resolved_dep_groups`: [3](#0-2) 

The struct comment even acknowledges only two categories: "If input or dep prev is cellbase, check that it's matured" — the dep-group container is a third category that was never added. [4](#0-3) 

### Impact Explanation
A miner can craft a cellbase output whose data is a valid molecule-encoded `OutVec` (a legal dep-group payload). Before the `cellbase_maturity` epoch window has elapsed, any transaction sender can reference that cellbase output as a `DepType::DepGroup` cell dep. The node resolves it into `resolved_dep_groups`, but `MaturityVerifier` never inspects that list, so the transaction passes maturity validation and enters the tx-pool or is committed to a block. This is a direct bypass of a consensus safety rule: the maturity window exists to protect against chain reorganisations that could invalidate the cellbase reward; allowing immature dep-group cells undermines that guarantee and can cause a consensus split between nodes that enforce the check and those that do not.

### Likelihood Explanation
The entry path requires only a cooperating miner (or a miner who mines their own cellbase) and a transaction sender. No privileged access, leaked keys, or majority hashpower is needed. The cellbase output data format (`OutVec` molecule encoding) is well-documented and straightforward to produce. The window of opportunity is the entire `cellbase_maturity` epoch period (4 epochs / ~16 hours on mainnet), giving an attacker ample time to exploit the gap.

### Recommendation
Add a third maturity check inside `MaturityVerifier::verify()` that iterates over `self.transaction.resolved_dep_groups`:

```rust
if let Some(index) = self
    .transaction
    .resolved_dep_groups
    .iter()
    .position(cellbase_immature)
{
    return Err(TransactionError::CellbaseImmaturity {
        inner: TransactionErrorSource::CellDeps,
        index,
    }
    .into());
}
```

A corresponding unit test should be added alongside the existing `test_inputs_cellbase_maturity` and `test_deps_cellbase_maturity` tests in `verification/src/tests/transaction_verifier.rs`. [5](#0-4) 

### Proof of Concept

1. Mine block `B` at epoch `E`. The cellbase of `B` produces output `C` whose `data` field is a valid molecule `OutVec` pointing to any live code cell (e.g., the always-success cell).
2. At epoch `E + δ` where `δ < cellbase_maturity`, construct transaction `T`:
   - Any normal input (non-cellbase).
   - One `cell_dep` entry: `{ out_point: C, dep_type: DepGroup }`.
   - A lock script whose code is the cell referenced inside `C`.
3. Submit `T` via `send_transaction` RPC.
4. `resolve_transaction` resolves `C` into `resolved_dep_groups`; the inner code cell goes into `resolved_cell_deps`.
5. `MaturityVerifier::verify()` checks `resolved_cell_deps` (the inner code cell — not a cellbase, passes) and `resolved_inputs` (normal input, passes). It never checks `resolved_dep_groups`, so `C`'s immaturity is invisible.
6. `T` is accepted into the tx-pool and can be committed to a block, violating the cellbase maturity consensus rule. [6](#0-5) [7](#0-6)

### Citations

**File:** util/types/src/core/cell.rs (L203-214)
```rust
/// Transaction with resolved input cells.
#[derive(Debug, Clone, Eq)]
pub struct ResolvedTransaction {
    /// The transaction view.
    pub transaction: TransactionView,
    /// Resolved cell dependencies.
    pub resolved_cell_deps: Vec<CellMeta>,
    /// Resolved input cells.
    pub resolved_inputs: Vec<CellMeta>,
    /// Resolved dependency group cells.
    pub resolved_dep_groups: Vec<CellMeta>,
}
```

**File:** util/types/src/core/cell.rs (L815-833)
```rust
    if cell_dep.dep_type() == DepType::DepGroup.into() {
        let outpoint = cell_dep.out_point();
        let dep_group = cell_resolver(&outpoint, true)?;
        let data = dep_group
            .mem_cell_data
            .as_ref()
            .expect("Load cell meta must with data");
        let sub_out_points =
            parse_dep_group_data(data).map_err(|_| OutPointError::InvalidDepGroup(outpoint))?;

        *remaining_dep_slots = remaining_dep_slots
            .checked_sub(sub_out_points.len())
            .ok_or(OutPointError::OverMaxDepExpansionLimit)?;

        for sub_out_point in sub_out_points.into_iter() {
            resolved_cell_deps.push(cell_resolver(&sub_out_point, eager_load)?);
        }
        resolved_dep_groups.push(dep_group);
    } else {
```

**File:** verification/src/transaction_verifier.rs (L361-368)
```rust
/// MaturityVerifier
///
/// If input or dep prev is cellbase, check that it's matured
pub struct MaturityVerifier {
    transaction: Arc<ResolvedTransaction>,
    epoch: EpochNumberWithFraction,
    cellbase_maturity: EpochNumberWithFraction,
}
```

**File:** verification/src/transaction_verifier.rs (L383-425)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let cellbase_immature = |meta: &CellMeta| -> bool {
            meta.transaction_info
                .as_ref()
                .map(|info| {
                    info.block_number > 0 && info.is_cellbase() && {
                        let threshold =
                            self.cellbase_maturity.to_rational() + info.block_epoch.to_rational();
                        let current = self.epoch.to_rational();
                        current < threshold
                    }
                })
                .unwrap_or(false)
        };

        if let Some(index) = self
            .transaction
            .resolved_inputs
            .iter()
            .position(cellbase_immature)
        {
            return Err(TransactionError::CellbaseImmaturity {
                inner: TransactionErrorSource::Inputs,
                index,
            }
            .into());
        }

        if let Some(index) = self
            .transaction
            .resolved_cell_deps
            .iter()
            .position(cellbase_immature)
        {
            return Err(TransactionError::CellbaseImmaturity {
                inner: TransactionErrorSource::CellDeps,
                index,
            }
            .into());
        }

        Ok(())
    }
```

**File:** verification/src/tests/transaction_verifier.rs (L282-339)
```rust
// deps immature verify
#[test]
pub fn test_deps_cellbase_maturity() {
    let transaction = TransactionBuilder::default().build();
    let output = CellOutput::new_builder()
        .capacity(capacity_bytes!(50))
        .build();

    let base_epoch = EpochNumberWithFraction::new(0, 0, 10);
    let cellbase_maturity = EpochNumberWithFraction::new(5, 0, 1);

    // The 1st dep is cellbase, the 2nd one is not.
    let rtx = Arc::new(ResolvedTransaction {
        transaction,
        resolved_cell_deps: vec![
            CellMetaBuilder::from_cell_output(output.clone(), Bytes::new())
                .transaction_info(mock_transaction_info(30, base_epoch, 0))
                .build(),
            CellMetaBuilder::from_cell_output(output, Bytes::new())
                .transaction_info(mock_transaction_info(40, base_epoch, 1))
                .build(),
        ],
        resolved_inputs: Vec::new(),
        resolved_dep_groups: vec![],
    });

    let mut current_epoch = EpochNumberWithFraction::new(0, 0, 10);
    let threshold = cellbase_maturity.to_rational() + base_epoch.to_rational();
    while current_epoch.number() < cellbase_maturity.number() + base_epoch.number() + 5 {
        let verifier = MaturityVerifier::new(Arc::clone(&rtx), current_epoch, cellbase_maturity);
        let current = current_epoch.to_rational();
        if current < threshold {
            assert_error_eq!(
                verifier.verify().unwrap_err(),
                TransactionError::CellbaseImmaturity {
                    inner: TransactionErrorSource::CellDeps,
                    index: 0
                },
                "base_epoch = {base_epoch}, current_epoch = {current_epoch}, cellbase_maturity = {cellbase_maturity}"
            );
        } else {
            assert!(
                verifier.verify().is_ok(),
                "base_epoch = {base_epoch}, current_epoch = {current_epoch}, cellbase_maturity = {cellbase_maturity}"
            );
        }
        {
            let number = current_epoch.number();
            let length = current_epoch.length();
            let index = current_epoch.index();
            current_epoch = if index == length {
                EpochNumberWithFraction::new(number + 1, 0, length)
            } else {
                EpochNumberWithFraction::new(number, index + 1, length)
            };
        }
    }
}
```
