Audit Report

## Title
`MaturityVerifier::verify()` Omits Cellbase Maturity Check on `resolved_dep_groups`, Enabling Immature Cellbase Use as DepGroup Dependency — (File: verification/src/transaction_verifier.rs)

## Summary
`MaturityVerifier::verify()` enforces the cellbase maturity rule on `resolved_inputs` and `resolved_cell_deps` but never iterates `resolved_dep_groups`. Because `resolve_transaction_dep` places the dep-group container cell exclusively into `resolved_dep_groups` (not `resolved_cell_deps`), a miner can reference their own immature cellbase output as a `DepType::DepGroup` cell dependency and have it accepted by nodes running this code, while correctly-implemented nodes would reject it — producing a consensus split.

## Finding Description
`ResolvedTransaction` carries three distinct `Vec<CellMeta>` fields: `resolved_inputs`, `resolved_cell_deps`, and `resolved_dep_groups`. [1](#0-0) 

When `resolve_transaction_dep` processes a `DepType::DepGroup` entry, the container cell is pushed into `resolved_dep_groups` and its expanded sub-cells are pushed into `resolved_cell_deps`: [2](#0-1) 

`MaturityVerifier::verify()` applies the `cellbase_immature` closure only to `resolved_inputs` and `resolved_cell_deps`: [3](#0-2) 

`resolved_dep_groups` is never iterated. The function returns `Ok(())` at line 424 without ever inspecting the container cell. No other guard in the codebase compensates for this omission — a search for any cross-check between `resolved_dep_groups` and cellbase maturity returns no results.

## Impact Explanation
The cellbase maturity rule is a consensus invariant: no transaction may reference an immature coinbase cell as an input or cell dependency. Bypassing it for dep-group container cells means nodes running this code accept a transaction that a correctly-implemented node rejects, causing a **consensus split**. This maps directly to the allowed Critical impact: *"Vulnerabilities which could easily cause consensus deviation."*

## Likelihood Explanation
The attacker must be a miner (or collude with one) to produce a cellbase whose output data encodes a valid `OutPointVec`. Miners control cellbase output data by design, making this a low-effort construction. Once the block is mined, the miner immediately submits the transaction via the standard `send_transaction` RPC before the maturity epoch elapses. No special privileges beyond mining a block are required, and the exploit is repeatable every block the attacker mines.

## Recommendation
Extend `MaturityVerifier::verify()` to iterate `resolved_dep_groups` with the same `cellbase_immature` closure before returning `Ok(())`:

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
``` [4](#0-3) 

This is symmetric with the existing `resolved_cell_deps` block and closes the gap entirely.

## Proof of Concept
1. Mine block N; the coinbase output at index 0 is an immature cellbase (`block_number = N > 0`, maturity epoch not yet reached).
2. At mining time, encode a valid `OutPointVec` (pointing to any live cells) into the cellbase output data.
3. Before `cellbase_maturity + block_epoch(N)` is reached, submit via `send_transaction` RPC a transaction with `cell_deps: [{ out_point: cellbase_outpoint(N, 0), dep_type: DepGroup }]` and any valid input/output.
4. `resolve_transaction_dep` places the cellbase `CellMeta` into `resolved_dep_groups` and the sub-cells into `resolved_cell_deps`.
5. `MaturityVerifier::verify()` checks `resolved_inputs` (no cellbase) and `resolved_cell_deps` (sub-cells only) — both pass. `resolved_dep_groups` is never checked.
6. The transaction is accepted into the tx-pool and committed, violating the cellbase maturity consensus rule and causing a consensus split with nodes that enforce the rule on all three collections.

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

**File:** verification/src/transaction_verifier.rs (L398-425)
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
