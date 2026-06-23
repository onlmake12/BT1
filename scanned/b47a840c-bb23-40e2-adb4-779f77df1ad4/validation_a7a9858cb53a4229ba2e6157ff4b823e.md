### Title
`MaturityVerifier` Does Not Check `resolved_dep_groups` for Cellbase Immaturity — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`MaturityVerifier::verify()` enforces the cellbase maturity rule on `resolved_inputs` and `resolved_cell_deps`, but silently omits `resolved_dep_groups`. A transaction sender can reference an immature cellbase output as a `DepGroup`-typed cell dep, bypassing the maturity check entirely.

---

### Finding Description

`ResolvedTransaction` holds three distinct cell collections: [1](#0-0) 

When a `CellDep` has `dep_type = DepGroup`, resolution pushes the dep-group cell itself into `resolved_dep_groups` and its expanded member cells into `resolved_cell_deps`: [2](#0-1) 

`MaturityVerifier::verify()` applies the `cellbase_immature` predicate to `resolved_inputs` and `resolved_cell_deps`, but never to `resolved_dep_groups`: [3](#0-2) 

The `cellbase_immature` closure is defined locally and never reused for `resolved_dep_groups`. There is no `TransactionErrorSource::DepGroups` variant and no corresponding check. If a cellbase output (block number > 0, within the maturity window) is used as the dep-group cell itself, the maturity gate is never applied to it.

---

### Impact Explanation

The cellbase maturity rule exists to prevent any use of immature block-reward outputs before they are considered settled. Bypassing it for dep-group cells violates this invariant at the consensus layer. Nodes that enforce the check on dep groups (e.g., after a fix) would reject such transactions, while current nodes accept them, creating a potential consensus split. Additionally, it undermines the economic security assumption that newly minted CKB cannot be referenced in protocol-significant ways until maturity.

---

### Likelihood Explanation

Any unprivileged transaction sender can craft a transaction that points a `CellDep` with `dep_type = DepGroup` at a recent cellbase output whose data happens to be a valid `OutPointVec`. The RPC entry point `send_transaction` accepts this without restriction. The condition is easy to satisfy on any live network where cellbase outputs exist and the maturity window has not elapsed.

---

### Recommendation

Extend `MaturityVerifier::verify()` to also iterate over `resolved_dep_groups`:

```rust
if let Some(index) = self
    .transaction
    .resolved_dep_groups
    .iter()
    .position(cellbase_immature)
{
    return Err(TransactionError::CellbaseImmaturity {
        inner: TransactionErrorSource::CellDeps, // or add DepGroups variant
        index,
    }
    .into());
}
```

Add a corresponding `TransactionErrorSource::DepGroups` variant if precise error attribution is desired, and add a unit test mirroring `test_deps_cellbase_maturity` but using a dep-group cell.

---

### Proof of Concept

1. Mine block N. The cellbase of block N produces output `(tx_N_hash, 0)` with capacity sufficient to hold an `OutPointVec`.
2. Craft a cell whose data is a valid `OutPointVec` pointing to any live cell (e.g., a system cell). This cell is the immature cellbase output itself, or a cell funded by it that is also immature.
3. Before the maturity epoch elapses, submit a transaction via `send_transaction` RPC with:
   - A normal live-cell input.
   - A `CellDep { out_point: (tx_N_hash, 0), dep_type: DepGroup }`.
4. `resolve_transaction_dep` pushes `(tx_N_hash, 0)` into `resolved_dep_groups`.
5. `MaturityVerifier::verify()` checks `resolved_cell_deps` (the expanded members) and `resolved_inputs`, but skips `resolved_dep_groups`.
6. The transaction passes maturity verification despite referencing an immature cellbase output as a dep group. [4](#0-3) [5](#0-4)

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

**File:** util/types/src/core/cell.rs (L807-841)
```rust
fn resolve_transaction_dep<F: FnMut(&OutPoint, bool) -> Result<CellMeta, OutPointError>>(
    cell_dep: &CellDep,
    cell_resolver: &mut F,
    resolved_cell_deps: &mut Vec<CellMeta>,
    resolved_dep_groups: &mut Vec<CellMeta>,
    eager_load: bool,
    remaining_dep_slots: &mut usize,
) -> Result<(), OutPointError> {
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
        *remaining_dep_slots = remaining_dep_slots
            .checked_sub(1)
            .ok_or(OutPointError::OverMaxDepExpansionLimit)?;

        resolved_cell_deps.push(cell_resolver(&cell_dep.out_point(), eager_load)?);
    }
    Ok(())
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
