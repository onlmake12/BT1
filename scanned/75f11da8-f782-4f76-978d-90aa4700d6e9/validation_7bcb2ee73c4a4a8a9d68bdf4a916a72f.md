### Title
`MaturityVerifier` Skips Immature Cellbase Check for `resolved_dep_groups`, Allowing Cellbase Maturity Bypass via DepGroup Path — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`MaturityVerifier::verify()` enforces the cellbase maturity rule against `resolved_inputs` and `resolved_cell_deps`, but never checks `resolved_dep_groups`. When a cell dep is declared with `DepType::DepGroup`, the container cell is placed into `resolved_dep_groups` (not `resolved_cell_deps`). A miner who crafts a cellbase output whose data is a valid `OutPointVec` can reference that immature cellbase as a `DepGroup` dep in a transaction, and the maturity check will not fire. This is the direct CKB analog of the VotingEscrow `ownershipChange` bypass: a guard applied on one code path (`Code` dep → `resolved_cell_deps`) is silently absent on the alternative path (`DepGroup` dep → `resolved_dep_groups`).

---

### Finding Description

**Root cause — `MaturityVerifier::verify()` omits `resolved_dep_groups`:**

`verification/src/transaction_verifier.rs` lines 383–425:

```rust
pub fn verify(&self) -> Result<(), Error> {
    let cellbase_immature = |meta: &CellMeta| -> bool { ... };

    // checked
    if let Some(index) = self.transaction.resolved_inputs.iter().position(cellbase_immature) {
        return Err(TransactionError::CellbaseImmaturity { inner: TransactionErrorSource::Inputs, index }.into());
    }
    // checked
    if let Some(index) = self.transaction.resolved_cell_deps.iter().position(cellbase_immature) {
        return Err(TransactionError::CellbaseImmaturity { inner: TransactionErrorSource::CellDeps, index }.into());
    }
    // *** resolved_dep_groups is NEVER checked ***
    Ok(())
}
```

**Why `resolved_dep_groups` is a distinct bucket:**

`util/types/src/core/cell.rs` lines 203–214 defines `ResolvedTransaction` with three separate `Vec<CellMeta>` fields: `resolved_inputs`, `resolved_cell_deps`, and `resolved_dep_groups`.

`resolve_transaction_dep` (lines 807–841) routes cells differently depending on `dep_type`:

```rust
if cell_dep.dep_type() == DepType::DepGroup.into() {
    let dep_group = cell_resolver(&outpoint, true)?;   // container cell
    // sub-cells go to resolved_cell_deps
    for sub_out_point in sub_out_points { resolved_cell_deps.push(...); }
    resolved_dep_groups.push(dep_group);               // container → dep_groups bucket
} else {
    resolved_cell_deps.push(cell_resolver(&cell_dep.out_point(), eager_load)?);
}
```

The container cell of a `DepGroup` dep lands exclusively in `resolved_dep_groups`. `MaturityVerifier` never iterates that field.

**The bypass path:**

1. Miner mines block N, producing a cellbase output whose `data` field is a valid `OutPointVec` (a molecule-encoded list of out-points pointing to any live code cells).
2. Before epoch `block_epoch + cellbase_maturity` is reached, the miner (or any user) submits a transaction with a `cell_dep` referencing that cellbase output with `dep_type = DepGroup`.
3. `resolve_transaction_dep` resolves the cellbase as the dep group container → `resolved_dep_groups`. The sub-cells (mature code cells) → `resolved_cell_deps`.
4. `MaturityVerifier::verify()` checks `resolved_cell_deps` (sub-cells, mature → passes) and `resolved_inputs` (unrelated → passes). It never touches `resolved_dep_groups`.
5. The immature cellbase container passes verification. The transaction is admitted to the pool and committed.

The error type documentation at `util/types/src/core/error.rs` line 168 explicitly states the rule covers both inputs and deps: *"It does not allow using an immature cell as input out-point and dependency out-point."* The `DepGroup` container is a dependency out-point that is not covered.

---

### Impact Explanation

A miner can reference their own immature cellbase output as a `DepGroup` container in a committed transaction, violating the consensus cellbase maturity rule. The maturity rule exists to ensure that cellbase outputs used as dependencies are stable (past the reorg-risk window). Bypassing it for dep group containers means:

- A committed transaction can carry a dependency on a cellbase that is still within the reorg window.
- If the cellbase block is reorganized, the dependent transaction is also invalidated, potentially causing unexpected rollbacks for users who accepted the transaction as confirmed.
- The rule stated in the protocol documentation and enforced by the error type is inconsistently applied, creating a divergence between the intended and actual consensus behavior.

Severity: **Medium** — consensus rule bypass reachable by an in-scope attacker (miner), with concrete protocol-level impact (rule violation, reorg-induced instability for dependent transactions).

---

### Likelihood Explanation

Any participant who mines even a single block can craft a cellbase with valid `OutPointVec` data. No majority hashpower is required — one block suffices. The miner then submits a transaction referencing that cellbase as a `DepGroup` dep before maturity. The attack is deterministic and requires no cryptographic break, no privileged key, and no social engineering. The only prerequisite is mining one block, which is a normal, permissionless activity explicitly listed as an in-scope attacker role.

---

### Recommendation

Add a third maturity check in `MaturityVerifier::verify()` covering `resolved_dep_groups`:

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

This mirrors the existing pattern for `resolved_cell_deps` and closes the bypass path. The `TransactionErrorSource` enum may also benefit from a dedicated `DepGroups` variant to improve diagnostic precision.

---

### Proof of Concept

1. **Setup:** Configure a CKB devnet with `cellbase_maturity = 4` epochs.

2. **Mine a crafted cellbase:** Use the block-template RPC to produce a block whose cellbase output at index 1 has `data` = a valid molecule-encoded `OutPointVec` containing the out-point of any live code cell (e.g., the always-success system cell).

3. **Submit the bypass transaction (before maturity):** Within the same epoch the cellbase was mined, submit a transaction:
   ```
   cell_deps: [{ out_point: <cellbase_tx_hash, 1>, dep_type: "dep_group" }]
   inputs:    [any spendable cell]
   outputs:   [any valid output]
   ```

4. **Observe:** The node accepts the transaction into the pool and commits it. No `CellbaseImmaturity` error is returned, even though the cellbase container cell is immature.

5. **Confirm the gap:** Repeat with `dep_type: "code"` pointing to the same cellbase output. The node correctly rejects it with `CellbaseImmaturity(CellDeps[0])`, confirming the check fires for `resolved_cell_deps` but not for `resolved_dep_groups`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

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
