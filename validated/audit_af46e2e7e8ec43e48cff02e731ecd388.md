All code references check out exactly as claimed. The vulnerability is confirmed.

Audit Report

## Title
`MaturityVerifier::verify()` Omits `resolved_dep_groups` Cellbase Maturity Check, Enabling Bypass via DepGroup Path — (`File: verification/src/transaction_verifier.rs`)

## Summary
`MaturityVerifier::verify()` enforces the cellbase maturity rule against `resolved_inputs` and `resolved_cell_deps` but never checks `resolved_dep_groups`. Because `resolve_transaction_dep` routes the container cell of any `DepType::DepGroup` dependency exclusively into `resolved_dep_groups`, an attacker can reference an immature cellbase output as a dep group container and bypass the maturity check entirely, allowing a consensus-violating transaction to be committed.

## Finding Description
`MaturityVerifier::verify()` defines the `cellbase_immature` closure and applies it to exactly two collections:

- `resolved_inputs` [1](#0-0) 
- `resolved_cell_deps` [2](#0-1) 

The function then returns `Ok(())` at line 424 without ever iterating `resolved_dep_groups`. [3](#0-2) 

`ResolvedTransaction` holds three distinct `Vec<CellMeta>` fields — `resolved_cell_deps`, `resolved_inputs`, and `resolved_dep_groups` — all of which can contain cellbase-originated cells. [4](#0-3) 

`resolve_transaction_dep` routes cells as follows for a `DepType::DepGroup` dep: sub-cells of the group go to `resolved_cell_deps` (line 830), while the container cell itself goes to `resolved_dep_groups` (line 832). [5](#0-4) 

For a plain `Code` dep, the cell goes to `resolved_cell_deps` (line 838), where the maturity check fires normally. [6](#0-5) 

The error type documentation explicitly states the rule covers both input out-points and dependency out-points, confirming the container dep-group cell is an unchecked dependency out-point. [7](#0-6) 

**Exploit path:**
1. Miner mines block N; cellbase output index 1 has `data` = a valid molecule-encoded `OutPointVec` pointing to any live code cell.
2. Before the cellbase maturity epoch threshold, the miner submits a transaction with `cell_deps: [{ out_point: <cellbase_tx_hash, 1>, dep_type: "dep_group" }]`.
3. `resolve_transaction_dep` places the immature cellbase into `resolved_dep_groups`; the sub-cells (mature) go to `resolved_cell_deps`.
4. `MaturityVerifier::verify()` checks `resolved_cell_deps` (sub-cells, mature → passes) and `resolved_inputs` (unrelated → passes). `resolved_dep_groups` is never checked.
5. The transaction is accepted and committed, violating the cellbase maturity consensus rule.

## Impact Explanation
The cellbase maturity rule is a consensus rule. A transaction that violates it being committed to a block constitutes an inconsistent application of consensus rules. Any node that correctly enforces the rule after a patch would reject a block containing such a transaction while unpatched nodes accept it — a direct chain fork condition. This matches the Critical allowed impact: **consensus deviation (15001–25000 points)**.

## Likelihood Explanation
Any participant who mines a single block can execute this attack. No majority hashpower is required. The attack is deterministic, requires no cryptographic break, no privileged key, and no social engineering. The only prerequisite is mining one block, which is a normal, permissionless activity. The bypass is repeatable and requires no victim interaction.

## Recommendation
Add a third maturity check in `MaturityVerifier::verify()` covering `resolved_dep_groups`, mirroring the existing pattern:

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

Optionally, introduce a dedicated `TransactionErrorSource::DepGroups` variant for diagnostic precision.

## Proof of Concept
1. Configure a CKB devnet with `cellbase_maturity = 4` epochs.
2. Mine a block whose cellbase output at index 1 has `data` = a valid molecule-encoded `OutPointVec` containing the out-point of any live code cell (e.g., the always-success system cell).
3. Within the same epoch, submit a transaction: `cell_deps: [{ out_point: <cellbase_tx_hash, 1>, dep_type: "dep_group" }]`, with any spendable input and valid output.
4. **Expected (buggy) result:** Node accepts the transaction — no `CellbaseImmaturity` error.
5. **Differential confirmation:** Repeat with `dep_type: "code"` pointing to the same cellbase output. Node correctly rejects with `CellbaseImmaturity(CellDeps[0])`.
6. The gap between the two responses confirms the check fires for `resolved_cell_deps` but is absent for `resolved_dep_groups`.

### Citations

**File:** verification/src/transaction_verifier.rs (L398-409)
```rust
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
```

**File:** verification/src/transaction_verifier.rs (L411-422)
```rust
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
```

**File:** verification/src/transaction_verifier.rs (L424-425)
```rust
        Ok(())
    }
```

**File:** util/types/src/core/cell.rs (L205-214)
```rust
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

**File:** util/types/src/core/cell.rs (L815-832)
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
```

**File:** util/types/src/core/cell.rs (L833-839)
```rust
    } else {
        *remaining_dep_slots = remaining_dep_slots
            .checked_sub(1)
            .ok_or(OutPointError::OverMaxDepExpansionLimit)?;

        resolved_cell_deps.push(cell_resolver(&cell_dep.out_point(), eager_load)?);
    }
```

**File:** util/types/src/core/error.rs (L164-172)
```rust
    /// The transaction is not mature yet, according to the cellbase maturity rule.
    #[error("CellbaseImmaturity({inner}[{index}])")]
    CellbaseImmaturity {
        /// The transaction field that causes the error.
        /// It should be `TransactionErrorSource::Inputs` or `TransactionErrorSource::CellDeps`. It does not allow using an immature cell as input out-point and dependency out-point.
        inner: TransactionErrorSource,
        /// The index of immature input out-point or dependency out-point.
        index: usize,
    },
```
