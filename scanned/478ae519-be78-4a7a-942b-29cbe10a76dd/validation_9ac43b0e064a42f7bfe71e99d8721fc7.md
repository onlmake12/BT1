### Title
`MaturityVerifier` Skips Cellbase Maturity Check on Dep-Group Container Cells — (`File: verification/src/transaction_verifier.rs`)

### Summary

`MaturityVerifier::verify()` enforces the cellbase maturity rule on `resolved_inputs` and `resolved_cell_deps`, but completely omits the check on `resolved_dep_groups`. A `ResolvedTransaction` has three distinct cell collections; dep-group container cells land in `resolved_dep_groups`, not in `resolved_cell_deps`. Any transaction that references an immature cellbase output as a `DepGroup` dependency bypasses the maturity gate entirely, violating a core protocol invariant.

### Finding Description

`ResolvedTransaction` holds three separate `Vec<CellMeta>` fields:

- `resolved_inputs` — consumed input cells
- `resolved_cell_deps` — code/data cells (including cells expanded *out of* dep groups)
- `resolved_dep_groups` — the dep-group *container* cells themselves [1](#0-0) 

When a `CellDep` with `dep_type = DepGroup` is resolved, `resolve_transaction_dep` pushes the container cell into `resolved_dep_groups` and the cells it points to into `resolved_cell_deps`: [2](#0-1) 

`MaturityVerifier::verify()` applies the `cellbase_immature` closure to `resolved_inputs` and `resolved_cell_deps`, but never to `resolved_dep_groups`: [3](#0-2) 

The closure itself is correct — it checks `block_number > 0 && is_cellbase() && current < threshold` — but it is simply never called for the dep-group container collection. The doc-comment on the struct even says *"If input or dep prev is cellbase, check that it's matured"*, confirming the intent was to cover all dep types. [4](#0-3) 

**Exploit path:**

1. A miner produces block N whose cellbase output contains valid dep-group data (a molecule-encoded `OutPointVec` pointing to any live, mature cells).
2. Before the cellbase maturity period elapses (default: 4 epochs), any unprivileged transaction sender submits a transaction via `send_transaction` RPC that lists the cellbase's `OutPoint` as a `CellDep` with `dep_type = dep_group`.
3. `resolve_transaction` resolves the dep: the immature cellbase cell lands in `resolved_dep_groups`; the cells it points to (which are mature) land in `resolved_cell_deps`.
4. `MaturityVerifier::verify()` iterates `resolved_inputs` (no cellbase) and `resolved_cell_deps` (mature cells — passes). It never touches `resolved_dep_groups`.
5. The transaction clears maturity verification and enters the tx-pool / gets committed to a block, despite referencing an immature cellbase output.

### Impact Explanation

The cellbase maturity rule is a consensus-level invariant: no cellbase output may be used (as input or dependency) until it has aged by `cellbase_maturity` epochs. Bypassing it for dep-group containers means:

- An immature cellbase can be referenced as a live dependency in committed blocks, violating the protocol rule.
- Nodes that independently re-verify the block will accept it (because they run the same flawed `MaturityVerifier`), so the violation propagates chain-wide without causing a fork — it is silently accepted by all nodes.
- A miner can immediately make their block reward cell usable as a dep-group dependency, circumventing the economic delay the maturity rule is designed to enforce.

### Likelihood Explanation

The attacker preconditions are minimal:

- The miner role is reachable by any participant with sufficient hashpower (or even a single block on a private fork for testing).
- Crafting a cellbase output with valid dep-group data requires only knowledge of the molecule encoding format — no privileged keys or insider access.
- The transaction sender submitting the dep-group reference is any unprivileged RPC caller.

The gap has existed since dep-group support was introduced and is not gated by any hardfork switch, making it continuously exploitable on mainnet.

### Recommendation

Add a third maturity check in `MaturityVerifier::verify()` for `resolved_dep_groups`, mirroring the existing checks for inputs and cell-deps:

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

This closes the gap so that all three cell collections are uniformly subject to the maturity rule, consistent with the stated invariant.

### Proof of Concept

1. Mine block N; craft the cellbase output so its `data` field is a valid molecule `OutPointVec` pointing to any two mature live cells (e.g., genesis system cells).
2. Immediately (epoch < `cellbase_maturity`) submit via RPC:
   ```json
   {
     "cell_deps": [{
       "out_point": { "tx_hash": "<cellbase_tx_hash_of_block_N>", "index": "0x0" },
       "dep_type": "dep_group"
     }],
     ...
   }
   ```
3. Observe: the transaction is accepted into the tx-pool and committed. `MaturityVerifier` does not return `CellbaseImmaturity` because `resolved_dep_groups` is never iterated.
4. Confirm: replacing `dep_type` with `code` causes the same cellbase to be placed in `resolved_cell_deps`, where the check fires and the transaction is correctly rejected.

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
