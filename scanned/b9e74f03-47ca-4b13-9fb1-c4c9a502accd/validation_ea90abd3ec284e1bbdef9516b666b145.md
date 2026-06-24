Audit Report

## Title
`MaturityVerifier::verify()` Omits Cellbase Maturity Check on `resolved_dep_groups` Container Cells - (File: verification/src/transaction_verifier.rs)

## Summary

`MaturityVerifier::verify()` enforces the cellbase maturity rule on `resolved_inputs` and `resolved_cell_deps` but never iterates `resolved_dep_groups`. When a `DepType::DepGroup` cell dependency is resolved, `resolve_transaction_dep` places the container cell exclusively into `resolved_dep_groups` and its sub-cells into `resolved_cell_deps`. A miner who encodes a valid `OutPointVec` into their own coinbase output data can immediately reference that immature cellbase as a `DepType::DepGroup` dependency, and the maturity check silently passes.

## Finding Description

`ResolvedTransaction` carries three distinct `Vec<CellMeta>` collections: [1](#0-0) 

`resolve_transaction_dep` routes the dep-group container cell into `resolved_dep_groups` and its expanded sub-cells into `resolved_cell_deps`: [2](#0-1) 

`MaturityVerifier::verify()` applies `cellbase_immature` only to `resolved_inputs` and `resolved_cell_deps`; `resolved_dep_groups` is never visited: [3](#0-2) 

The struct's own doc comment states the verifier covers "input or dep prev", and the sub-cells in `resolved_cell_deps` are checked, but the container cell that actually carries the cellbase output is not. A miner who controls coinbase output data can encode a valid `OutPointVec` there, then submit a transaction with `dep_type: DepGroup` pointing at that cellbase before the maturity epoch elapses. `resolve_transaction_dep` loads the container cell, parses the sub-out-points, and pushes the container into `resolved_dep_groups`. The sub-cells (which are ordinary live cells) pass the `resolved_cell_deps` check; the immature cellbase container in `resolved_dep_groups` is never examined. [4](#0-3) 

## Impact Explanation

The cellbase maturity rule is a consensus invariant. Bypassing it for dep-group container cells means a miner can commit a transaction that all current nodes accept but that violates the protocol specification. Any future node version that correctly enforces the rule on all three collections would reject the same transaction, producing a **consensus split** — a Critical-severity impact matching "Vulnerabilities which could easily cause consensus deviation."

## Likelihood Explanation

The attacker must be a miner (or collude with one) to produce a coinbase output whose data is a valid serialized `OutPointVec`. This is low-effort: the miner controls coinbase output data at block-production time. Once the block is mined, the miner immediately submits the crafted transaction via the standard `send_transaction` RPC before the maturity epoch is reached. No victim interaction is required; the entry path is the normal tx-pool submission interface. [5](#0-4) 

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

This is symmetric with the existing `resolved_cell_deps` block and requires no changes to any other component. [6](#0-5) 

## Proof of Concept

1. Configure a test chain with a short `cellbase_maturity` (e.g., 5 epochs).
2. Mine block N; at mining time, set the coinbase output data to a valid serialized `OutPointVec` pointing at any two live cells.
3. Immediately (before epoch `block_epoch(N) + cellbase_maturity`) submit a transaction:
   - `cell_deps: [{ out_point: (txhash_of_coinbase_N, 0), dep_type: DepGroup }]`
   - Any valid input consuming a mature cell, any valid output.
4. Observe that `resolve_transaction_dep` pushes the coinbase `CellMeta` into `resolved_dep_groups` and the two sub-cells into `resolved_cell_deps`.
5. `MaturityVerifier::verify()` checks `resolved_inputs` (no cellbase) and `resolved_cell_deps` (mature sub-cells) — both pass. `resolved_dep_groups` (immature coinbase container) is never checked.
6. The transaction is accepted into the tx-pool and committed, violating the cellbase maturity consensus rule. [7](#0-6)

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

**File:** verification/src/transaction_verifier.rs (L361-364)
```rust
/// MaturityVerifier
///
/// If input or dep prev is cellbase, check that it's matured
pub struct MaturityVerifier {
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

**File:** test/src/specs/tx_pool/cellbase_maturity.rs (L1-48)
```rust
use crate::utils::assert_send_transaction_fail;
use crate::{DEFAULT_TX_PROPOSAL_WINDOW, Node, Spec};

use ckb_logger::info;
use ckb_types::core::BlockNumber;

const MATURITY: BlockNumber = 5;

pub struct CellbaseMaturity;

impl Spec for CellbaseMaturity {
    fn run(&self, nodes: &mut Vec<Node>) {
        let node = &nodes[0];

        info!("Generate DEFAULT_TX_PROPOSAL_WINDOW.1 + 2 block");
        node.mine_until_out_bootstrap_period();

        info!("Use generated block's cellbase as tx input");
        let tip_block = node.get_tip_block();
        let tx = node.new_transaction(tip_block.transactions()[0].hash());

        (0..MATURITY - DEFAULT_TX_PROPOSAL_WINDOW.0).for_each(|i| {
            info!("Tx is not maturity in N + {} block", i);
            assert_send_transaction_fail(node, &tx, "CellbaseImmaturity");
            node.mine(1);
        });

        info!(
            "Tx will be added to pending pool in N + {} block",
            MATURITY - DEFAULT_TX_PROPOSAL_WINDOW.0
        );
        let tx_hash = node.rpc_client().send_transaction(tx.data().into());
        assert_eq!(tx_hash, tx.hash());
        node.assert_tx_pool_size(1, 0);

        info!(
            "Tx will be added to proposed pool in N + {} block",
            MATURITY
        );
        node.mine(DEFAULT_TX_PROPOSAL_WINDOW.0);
        node.assert_tx_pool_size(0, 1);
        node.mine(1);
        node.assert_tx_pool_size(0, 0);
    }

    fn modify_chain_spec(&self, spec: &mut ckb_chain_spec::ChainSpec) {
        spec.params.cellbase_maturity = Some(MATURITY);
    }
```
