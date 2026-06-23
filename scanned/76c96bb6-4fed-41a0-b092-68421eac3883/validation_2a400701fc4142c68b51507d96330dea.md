### Title
`MaturityVerifier` Skips Cellbase Maturity Check for `resolved_dep_groups` Container Cells, Allowing Immature Cellbase Use as DepGroup Dependency — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`MaturityVerifier::verify()` enforces the cellbase maturity rule on `resolved_inputs` and `resolved_cell_deps`, but entirely omits the check on `resolved_dep_groups`. A `ResolvedTransaction` carries three distinct cell collections; the dep-group container cells land exclusively in `resolved_dep_groups` and are never inspected for immaturity. A miner can therefore craft a transaction that references their own immature cellbase output as a `DepType::DepGroup` cell dependency, bypassing the consensus-mandated maturity lockup.

---

### Finding Description

`ResolvedTransaction` holds three separate `Vec<CellMeta>` fields:

- `resolved_inputs` — cells being consumed as inputs
- `resolved_cell_deps` — direct `DepType::Code` cells **and** the sub-cells expanded out of every dep group
- `resolved_dep_groups` — the **container** cells of every `DepType::DepGroup` dependency [1](#0-0) 

When `resolve_transaction_dep` processes a `DepType::DepGroup` entry, the container cell is pushed into `resolved_dep_groups` and its sub-cells are pushed into `resolved_cell_deps`: [2](#0-1) 

`MaturityVerifier::verify()` applies the `cellbase_immature` closure only to `resolved_inputs` and `resolved_cell_deps`: [3](#0-2) 

`resolved_dep_groups` is never iterated. If the dep-group container cell is itself an immature cellbase output (`block_number > 0`, `is_cellbase() == true`, epoch threshold not yet reached), the check silently passes.

---

### Impact Explanation

The cellbase maturity rule is a consensus invariant: no transaction may reference an immature coinbase cell as an input **or** as a cell dependency. Bypassing it for dep-group containers means:

1. A miner can immediately submit a transaction referencing their own block's cellbase as a `DepType::DepGroup` dep, before the maturity epoch is reached.
2. Nodes that correctly enforce the rule on all three collections would reject the transaction; nodes running this code would accept it — a **consensus split**.
3. The immature cellbase's data is loaded and parsed during dep-group resolution, so the cell is materially "used" in the transaction despite the lockup.

---

### Likelihood Explanation

The attacker must be a miner (or collude with one) to produce a cellbase output whose data encodes a valid dep-group payload (a serialized `OutPointVec`). This is a low-effort construction: the miner controls the cellbase output data. Once the block is mined, the miner immediately submits a transaction using that cellbase as a `DepType::DepGroup` dep before the maturity epoch elapses. The entry path is the standard `send_transaction` RPC / tx-pool submission, reachable by any unprivileged tx-pool submitter who also mines.

---

### Recommendation

Extend `MaturityVerifier::verify()` to iterate `resolved_dep_groups` with the same `cellbase_immature` closure, returning `CellbaseImmaturity` with an appropriate `TransactionErrorSource` variant (e.g., `CellDeps`) if any container cell is immature. The fix is a single additional iterator check, symmetric with the existing `resolved_cell_deps` block:

```rust
if let Some(index) = self
    .transaction
    .resolved_dep_groups          // ← add this block
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

---

### Proof of Concept

1. Mine block N; the coinbase output at index 0 is an immature cellbase (`block_number = N > 0`).
2. Encode a valid `OutPointVec` (pointing to any live cells) into the cellbase output data at mining time.
3. Before epoch `cellbase_maturity + block_epoch(N)` is reached, submit a transaction with:
   - `cell_deps: [{ out_point: cellbase_outpoint(N, 0), dep_type: DepGroup }]`
   - Any valid input and output.
4. `resolve_transaction` places the cellbase `CellMeta` into `resolved_dep_groups` and the sub-cells into `resolved_cell_deps`.
5. `MaturityVerifier::verify()` checks `resolved_inputs` (empty of cellbase) and `resolved_cell_deps` (sub-cells, not cellbase) — both pass. `resolved_dep_groups` is never checked.
6. The transaction is accepted into the tx-pool and committed, violating the cellbase maturity consensus rule. [4](#0-3) [5](#0-4)

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

**File:** util/types/src/core/cell.rs (L807-840)
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
