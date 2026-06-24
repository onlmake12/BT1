Audit Report

## Title
`MaturityVerifier::verify()` Omits `resolved_dep_groups` Cellbase Maturity Check, Enabling Bypass via DepGroup Path — (`File: verification/src/transaction_verifier.rs`)

## Summary
`MaturityVerifier::verify()` enforces the cellbase maturity consensus rule against `resolved_inputs` and `resolved_cell_deps` but never iterates `resolved_dep_groups`. Because `resolve_transaction_dep` routes the container cell of any `DepType::DepGroup` dependency exclusively into `resolved_dep_groups`, a miner can reference their own immature cellbase output as a dep group container and the maturity check will not fire. The transaction is accepted and committed, violating the cellbase maturity consensus rule.

## Finding Description
`MaturityVerifier::verify()` defines the `cellbase_immature` closure and applies it to exactly two collections:

- `self.transaction.resolved_inputs` (lines 398–409)
- `self.transaction.resolved_cell_deps` (lines 411–422)

`resolved_dep_groups` is never touched (line 424 returns `Ok(())`).

`ResolvedTransaction` (`util/types/src/core/cell.rs` lines 203–214) holds three distinct `Vec<CellMeta>` fields: `resolved_cell_deps`, `resolved_inputs`, and `resolved_dep_groups`.

`resolve_transaction_dep` (`util/types/src/core/cell.rs` lines 815–832) routes cells as follows when `dep_type == DepGroup`:
- Sub-cells of the group → `resolved_cell_deps` (line 830)
- The container cell itself → `resolved_dep_groups` (line 832)

For a plain `Code` dep, the cell goes to `resolved_cell_deps` (line 838), where the maturity check fires normally.

**Exploit path:**
1. Miner mines block N; cellbase output index 1 has `data` = a valid molecule-encoded `OutPointVec` pointing to any live code cell.
2. Before epoch `block_epoch + cellbase_maturity`, the miner submits a transaction with `cell_deps: [{ out_point: <cellbase_tx_hash, 1>, dep_type: "dep_group" }]`.
3. `resolve_transaction_dep` places the immature cellbase into `resolved_dep_groups`; the sub-cells (mature) go to `resolved_cell_deps`.
4. `MaturityVerifier::verify()` checks `resolved_cell_deps` (sub-cells, mature → passes) and `resolved_inputs` (unrelated → passes). `resolved_dep_groups` is never checked.
5. The transaction is accepted. The error type documentation at `util/types/src/core/error.rs` line 168 explicitly states: *"It does not allow using an immature cell as input out-point and dependency out-point"* — the container dep-group cell is a dependency out-point that is not covered.

The bypass is confirmed by the differential: submitting the same cellbase output with `dep_type: "code"` correctly triggers `CellbaseImmaturity(CellDeps[0])` because that path routes through `resolved_cell_deps`.

## Impact Explanation
The cellbase maturity rule is a consensus rule. Bypassing it for dep group containers means a committed transaction carries a dependency on a cellbase still within the reorg-risk window. If the cellbase block is reorganized, the dependent transaction is invalidated, causing unexpected rollbacks for users who accepted the transaction as confirmed. More critically, the maturity check is part of transaction validation used at the consensus layer; a transaction that violates this rule being committed to a block constitutes an inconsistent application of a consensus rule, matching the allowed impact: **consensus deviation** (Critical, 15001–25000 points). Any node that correctly enforces the rule (e.g., after a patch) would reject a block containing such a transaction, while unpatched nodes accept it — a direct fork condition.

## Likelihood Explanation
Any participant who mines a single block can craft a cellbase with valid `OutPointVec` data. No majority hashpower is required. The attack is deterministic, requires no cryptographic break, no privileged key, and no social engineering. The only prerequisite is mining one block, which is a normal, permissionless activity. The bypass is repeatable and requires no victim interaction.

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

Optionally, add a dedicated `TransactionErrorSource::DepGroups` variant for diagnostic precision.

## Proof of Concept
1. Configure a CKB devnet with `cellbase_maturity = 4` epochs.
2. Mine a block whose cellbase output at index 1 has `data` = a valid molecule-encoded `OutPointVec` containing the out-point of any live code cell (e.g., the always-success system cell).
3. Within the same epoch, submit a transaction: `cell_deps: [{ out_point: <cellbase_tx_hash, 1>, dep_type: "dep_group" }]`, with any spendable input and valid output.
4. **Expected (buggy) result:** Node accepts the transaction — no `CellbaseImmaturity` error.
5. **Differential confirmation:** Repeat with `dep_type: "code"` pointing to the same cellbase output. Node correctly rejects with `CellbaseImmaturity(CellDeps[0])`.
6. The gap between the two responses confirms the check fires for `resolved_cell_deps` but is absent for `resolved_dep_groups`.