The code confirms all claims in the report. Let me verify the key facts:

1. `ResolvedTransaction` has three collections confirmed at [1](#0-0) 

2. `resolve_transaction_dep` routes the container cell to `resolved_dep_groups` and sub-cells to `resolved_cell_deps` confirmed at [2](#0-1) 

3. `MaturityVerifier::verify()` only iterates `resolved_inputs` and `resolved_cell_deps`, never `resolved_dep_groups` confirmed at [3](#0-2) 

All code references are accurate and the gap is real.

---

Audit Report

## Title
`MaturityVerifier::verify()` Omits Cellbase Maturity Check on `resolved_dep_groups` Container Cells - (File: verification/src/transaction_verifier.rs)

## Summary
`MaturityVerifier::verify()` enforces the cellbase maturity rule on `resolved_inputs` and `resolved_cell_deps` but never checks `resolved_dep_groups`. When a `DepType::DepGroup` cell dependency is resolved, `resolve_transaction_dep` places the container cell exclusively into `resolved_dep_groups` and its sub-cells into `resolved_cell_deps`. A miner who encodes a valid `OutPointVec` into their coinbase output data can immediately reference that immature cellbase as a `DepType::DepGroup` dependency, and the maturity check silently passes, violating the cellbase maturity consensus invariant.

## Finding Description
`ResolvedTransaction` carries three distinct `Vec<CellMeta>` collections: `resolved_inputs`, `resolved_cell_deps`, and `resolved_dep_groups`. When `resolve_transaction_dep` processes a `DepType::DepGroup` cell dependency, it calls `cell_resolver` on the container out-point, pushes the container `CellMeta` into `resolved_dep_groups`, then iterates the parsed sub-out-points and pushes each into `resolved_cell_deps`. The container cell never enters `resolved_cell_deps`.

`MaturityVerifier::verify()` applies the `cellbase_immature` closure only to `resolved_inputs` and `resolved_cell_deps`. There is no third block iterating `resolved_dep_groups`. A miner who controls coinbase output data can serialize a valid `OutPointVec` pointing at any two live cells as the coinbase output data. Before the maturity epoch elapses, they submit a transaction with `cell_deps: [{out_point: (coinbase_txhash, 0), dep_type: DepGroup}]`. `resolve_transaction_dep` loads the immature coinbase `CellMeta` into `resolved_dep_groups` and the mature sub-cells into `resolved_cell_deps`. The maturity verifier checks `resolved_inputs` (no cellbase) and `resolved_cell_deps` (mature sub-cells) — both pass. The immature coinbase container in `resolved_dep_groups` is never examined, and the transaction is accepted.

## Impact Explanation
The cellbase maturity rule is a consensus invariant. Accepting a transaction that uses an immature cellbase as a dep-group container violates the protocol specification. Any node version that correctly enforces the rule on all three collections would reject the same transaction, producing a consensus split. This matches the Critical allowed impact: "Vulnerabilities which could easily cause consensus deviation."

## Likelihood Explanation
The attacker must be a miner or collude with one to produce a coinbase output whose data is a valid serialized `OutPointVec`. This is low-effort: the miner controls coinbase output data at block-production time. Once the block is mined, the miner immediately submits the crafted transaction via the standard `send_transaction` RPC before the maturity epoch is reached. No victim interaction is required; the entry path is the normal tx-pool submission interface.

## Recommendation
Add a third iterator check in `MaturityVerifier::verify()` that applies the same `cellbase_immature` closure to `resolved_dep_groups`, returning `CellbaseImmaturity` with `TransactionErrorSource::CellDeps` if any container cell is immature:

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

This is symmetric with the existing `resolved_cell_deps` block and requires no changes to any other component.

## Proof of Concept
1. Configure a test chain with a short `cellbase_maturity` (e.g., 5 epochs).
2. Mine block N; at mining time, set the coinbase output data to a valid serialized `OutPointVec` pointing at any two live cells.
3. Immediately (before epoch `block_epoch(N) + cellbase_maturity`) submit a transaction with `cell_deps: [{out_point: (txhash_of_coinbase_N, 0), dep_type: DepGroup}]` and any valid input consuming a mature cell.
4. Observe that `resolve_transaction_dep` pushes the coinbase `CellMeta` into `resolved_dep_groups` and the two sub-cells into `resolved_cell_deps`.
5. `MaturityVerifier::verify()` checks `resolved_inputs` (no cellbase) and `resolved_cell_deps` (mature sub-cells) — both pass. `resolved_dep_groups` (immature coinbase container) is never checked.
6. The transaction is accepted into the tx-pool and committed, violating the cellbase maturity consensus rule.

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

**File:** verification/src/transaction_verifier.rs (L398-422)
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
```
