Audit Report

## Title
`MaturityVerifier::verify()` Skips Cellbase Immaturity Check on `resolved_dep_groups` — (File: verification/src/transaction_verifier.rs)

## Summary

`MaturityVerifier::verify()` enforces the cellbase maturity rule on `resolved_inputs` and `resolved_cell_deps` but never iterates over `resolved_dep_groups`. When a `CellDep` with `dep_type = DepGroup` is resolved, the dep-group container cell is placed exclusively in `resolved_dep_groups`, so an immature cellbase output used as the dep-group container cell passes maturity verification unchecked. This violates the consensus-level cellbase maturity invariant and can cause a consensus split between patched and unpatched nodes.

## Finding Description

`ResolvedTransaction` holds three distinct `Vec<CellMeta>` collections: `resolved_cell_deps`, `resolved_inputs`, and `resolved_dep_groups`. [1](#0-0) 

In `resolve_transaction_dep`, when `dep_type == DepGroup`, the container cell is pushed into `resolved_dep_groups` (line 832) and its expanded member cells into `resolved_cell_deps` (line 830). [2](#0-1) 

`MaturityVerifier::verify()` applies `cellbase_immature` to `resolved_inputs` (lines 398–409) and `resolved_cell_deps` (lines 411–422), then returns `Ok(())` without ever touching `resolved_dep_groups`. [3](#0-2) 

`TransactionErrorSource` has no `DepGroups` variant, confirming the check was never implemented for this collection. [4](#0-3) 

Exploit path:
1. A miner produces block N whose cellbase output `(tx_N_hash, 0)` carries data that is a valid molecule-encoded `OutPointVec` pointing to any live cell.
2. Before the maturity epoch elapses, an attacker submits a transaction with a normal live-cell input and `CellDep { out_point: (tx_N_hash, 0), dep_type: DepGroup }`.
3. `resolve_transaction_dep` pushes `(tx_N_hash, 0)` into `resolved_dep_groups` and its member cells into `resolved_cell_deps`.
4. `MaturityVerifier::verify()` checks `resolved_cell_deps` (the members, which may be mature) and `resolved_inputs`, but skips `resolved_dep_groups`.
5. The transaction is accepted despite referencing an immature cellbase output.

## Impact Explanation

The cellbase maturity rule is a consensus rule: no immature cellbase output may be referenced in any protocol-significant way. Bypassing it for dep-group container cells means the rule is not uniformly enforced. Any node that applies a corrective patch would reject transactions that current unpatched nodes accept, producing a consensus split. This matches the **Critical (15001–25000 points)** impact class: *Vulnerabilities which could easily cause consensus deviation*.

## Likelihood Explanation

The attacker must control or collude with a miner to produce a cellbase output whose data field is a valid molecule-encoded `OutPointVec`. Miners freely choose the data field of their coinbase outputs, making this a non-trivial but realistic precondition. Once such an output exists, any unprivileged user can craft and submit the exploiting transaction via the `send_transaction` RPC before the maturity window closes. The condition is repeatable on any live network.

## Recommendation

Extend `MaturityVerifier::verify()` to iterate over `resolved_dep_groups` after the existing `resolved_cell_deps` check:

```rust
if let Some(index) = self
    .transaction
    .resolved_dep_groups
    .iter()
    .position(cellbase_immature)
{
    return Err(TransactionError::CellbaseImmaturity {
        inner: TransactionErrorSource::CellDeps, // or add a DepGroups variant
        index,
    }
    .into());
}
```

Optionally add a `TransactionErrorSource::DepGroups` variant for precise error attribution, and add a unit test mirroring the existing `test_deps_cellbase_maturity` pattern but constructing a `ResolvedTransaction` with an immature cellbase cell in `resolved_dep_groups`. [5](#0-4) 

## Proof of Concept

1. Mine block N. Set the cellbase output data to a valid molecule `OutPointVec` referencing any live cell (e.g., a genesis system cell).
2. Before the maturity epoch elapses, call `send_transaction` RPC with:
   - One normal live-cell input.
   - `cell_deps: [{ out_point: { tx_hash: <tx_N_hash>, index: 0 }, dep_type: "dep_group" }]`.
3. Observe that the node resolves `(tx_N_hash, 0)` into `resolved_dep_groups` and its member into `resolved_cell_deps`.
4. `MaturityVerifier::verify()` returns `Ok(())` — the transaction is accepted.

Unit test: in `verification/src/tests/transaction_verifier.rs`, construct a `ResolvedTransaction` with an immature cellbase `CellMeta` placed in `resolved_dep_groups` (and mature cells in `resolved_inputs`/`resolved_cell_deps`), invoke `MaturityVerifier::verify()`, and assert it currently returns `Ok(())` — demonstrating the missing check. [6](#0-5)

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

**File:** util/types/src/core/error.rs (L62-75)
```rust
pub enum TransactionErrorSource {
    /// cell deps
    CellDeps,
    /// header deps
    HeaderDeps,
    /// inputs
    Inputs,
    /// outputs
    Outputs,
    /// outputs data
    OutputsData,
    /// witnesses
    Witnesses,
}
```

**File:** verification/src/tests/transaction_verifier.rs (L1-28)
```rust
use super::super::transaction_verifier::{
    CapacityVerifier, DaoScriptSizeVerifier, DuplicateDepsVerifier, EmptyVerifier,
    MaturityVerifier, OutputsDataVerifier, Since, SinceVerifier, SizeVerifier, VersionVerifier,
};
use crate::error::TransactionErrorSource;
use crate::transaction_verifier::ScriptHashTypeVerifier;
use crate::{TransactionError, TxVerifyEnv};
use ckb_chain_spec::{
    OUTPUT_INDEX_DAO, build_genesis_type_id_script,
    consensus::{Consensus, ConsensusBuilder},
};
use ckb_error::{Error, assert_error_eq};
use ckb_test_chain_utils::{MOCK_MEDIAN_TIME_COUNT, MockMedianTime};
use ckb_traits::CellDataProvider;
use ckb_types::{
    bytes::Bytes,
    constants::TX_VERSION,
    core::{
        BlockNumber, Capacity, EpochNumber, EpochNumberWithFraction, HeaderView, ScriptHashType,
        TransactionBuilder, TransactionInfo, TransactionView, capacity_bytes,
        cell::{CellMetaBuilder, ResolvedTransaction},
        hardfork::HardForks,
    },
    h256,
    packed::{Byte32, CellDep, CellInput, CellOutput, OutPoint, Script},
    prelude::*,
};
use std::sync::Arc;
```
