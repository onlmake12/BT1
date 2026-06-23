### Title
`DuplicateDepsVerifier` Fails to Detect Same `out_point` Referenced as Both `Code` and `DepGroup` — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

The `DuplicateDepsVerifier` checks for duplicate `CellDep` entries using full struct equality (both `out_point` and `dep_type`). This means a transaction that references the same `out_point` once as `dep_type: Code` and once as `dep_type: DepGroup` passes the duplicate check, causing the same cell to be loaded into two different roles in `ResolvedTransaction`, producing inconsistent state that affects script execution.

---

### Finding Description

`DuplicateDepsVerifier::verify` builds a `HashSet<CellDep>` and uses `seen_cells.replace(dep)` to detect duplicates: [1](#0-0) 

`CellDep` is a molecule-generated fixed-size struct whose raw bytes encode both `out_point` (36 bytes) and `dep_type` (1 byte): [2](#0-1) 

Because `Hash` and `PartialEq` for `CellDep` are derived from the full byte slice, two entries with the same `out_point` but different `dep_type` values are treated as distinct elements. The `HashSet` never fires, and the transaction passes `NonContextualTransactionVerifier`: [3](#0-2) 

When `resolve_transaction_deps_with_system_cell_cache` subsequently processes the cell deps, it dispatches on `dep_type` per entry without any cross-entry deduplication by `out_point`: [4](#0-3) 

For the `Code` entry, cell `X` is pushed into `resolved_cell_deps`. For the `DepGroup` entry with the same `out_point` `X`, cell `X` is pushed into `resolved_dep_groups` **and** all sub-cells of `X` are pushed into `resolved_cell_deps`. Cell `X` therefore appears in both `resolved_cell_deps` and `resolved_dep_groups` simultaneously: [5](#0-4) 

---

### Impact Explanation

1. **Inconsistent `ResolvedTransaction` state**: The same cell occupies two structurally distinct roles (`resolved_cell_deps` as code, `resolved_dep_groups` as a group container). The sub-cells of the dep group are also injected into `resolved_cell_deps`, inflating the dependency set with cells the transaction author did not explicitly list.

2. **Liveness check bypass**: `ResolvedTransaction::check` uses a `checked_cells` HashSet keyed by `OutPoint` alone. Once `X` is checked as a code dep, it is skipped when encountered again as a dep group container, silently bypassing the liveness re-check for that role: [6](#0-5) 

3. **Script execution anomaly**: The CKB-VM script resolution layer iterates over `resolved_cell_deps` and `resolved_dep_groups` to match code hashes. Having `X` in both lists can trigger `MultipleMatches` errors for scripts that would otherwise resolve cleanly, or can cause a script to silently resolve against an unintended copy of the cell.

---

### Likelihood Explanation

Any unprivileged transaction sender can craft this condition by constructing a `RawTransaction` with two `CellDep` entries sharing the same `out_point` but differing in `dep_type`. The entry path is the standard `send_transaction` RPC. No special privilege, key material, or network position is required. The `NonContextualTransactionVerifier` is the only gate before the transaction enters the pool and is relayed to peers.

---

### Recommendation

Change `DuplicateDepsVerifier` to key the seen-set on `out_point` only, not on the full `CellDep` struct:

```rust
// current (misses same out_point with different dep_type)
let mut seen_cells = HashSet::<CellDep>::with_capacity(...);
if let Some(dep) = transaction.cell_deps_iter()
    .find_map(|dep| seen_cells.replace(dep)) { ... }

// fix: key on out_point alone
let mut seen_out_points = HashSet::<OutPoint>::with_capacity(...);
if let Some(dep) = transaction.cell_deps_iter()
    .find_map(|dep| {
        if seen_out_points.insert(dep.out_point()) { None } else { Some(dep) }
    }) { ... }
```

This mirrors the existing `out_point`-keyed deduplication already applied to transaction inputs in `resolve_transaction`: [7](#0-6) 

---

### Proof of Concept

```
Transaction {
  cell_deps: [
    CellDep { out_point: X, dep_type: 0x00 },   // Code
    CellDep { out_point: X, dep_type: 0x01 },   // DepGroup
  ],
  inputs: [...],
  ...
}
```

- `DuplicateDepsVerifier` sees two distinct `CellDep` byte sequences → no error.
- `resolve_transaction_deps_with_system_cell_cache` processes both entries:
  - First entry: `X` → `resolved_cell_deps`.
  - Second entry: `X` → `resolved_dep_groups`; sub-cells of `X` → `resolved_cell_deps`.
- `ResolvedTransaction.resolved_cell_deps` now contains `X` plus all sub-cells of `X`; `resolved_dep_groups` also contains `X`.
- Script execution encounters `X` in both lists, producing `MultipleMatches` or silently using the wrong resolution path. [1](#0-0) [8](#0-7)

### Citations

**File:** verification/src/transaction_verifier.rs (L94-102)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        self.version.verify()?;
        self.size.verify()?;
        self.empty.verify()?;
        self.duplicate_deps.verify()?;
        self.outputs_data_verifier.verify()?;
        self.script_hash_type.verify()?;
        Ok(())
    }
```

**File:** verification/src/transaction_verifier.rs (L437-458)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let transaction = self.transaction;
        let mut seen_cells = HashSet::with_capacity(self.transaction.cell_deps().len());
        let mut seen_headers = HashSet::with_capacity(self.transaction.header_deps().len());

        if let Some(dep) = transaction
            .cell_deps_iter()
            .find_map(|dep| seen_cells.replace(dep))
        {
            return Err(TransactionError::DuplicateCellDeps {
                out_point: dep.out_point(),
            }
            .into());
        }
        if let Some(hash) = transaction
            .header_deps_iter()
            .find_map(|hash| seen_headers.replace(hash))
        {
            return Err(TransactionError::DuplicateHeaderDeps { hash }.into());
        }
        Ok(())
    }
```

**File:** util/gen-types/src/generated/blockchain.rs (L7557-7574)
```rust
impl CellDep {
    const DEFAULT_VALUE: [u8; 37] = [
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 0, 0, 0, 0, 0, 0,
    ];
    pub const TOTAL_SIZE: usize = 37;
    pub const FIELD_SIZES: [usize; 2] = [36, 1];
    pub const FIELD_COUNT: usize = 2;
    pub fn out_point(&self) -> OutPoint {
        OutPoint::new_unchecked(self.0.slice(0..36))
    }
    pub fn dep_type(&self) -> Byte {
        Byte::new_unchecked(self.0.slice(36..37))
    }
    pub fn as_reader<'r>(&'r self) -> CellDepReader<'r> {
        CellDepReader::new_unchecked(self.as_slice())
    }
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

**File:** util/types/src/core/cell.rs (L315-376)
```rust
        let mut checked_cells: HashSet<OutPoint> = HashSet::new();
        let mut check_cell = |out_point: &OutPoint| -> Result<(), OutPointError> {
            if seen_inputs.contains(out_point) {
                return Err(OutPointError::Dead(out_point.clone()));
            }

            if checked_cells.contains(out_point) {
                return Ok(());
            }

            match cell_checker.is_live(out_point) {
                Some(true) => {
                    checked_cells.insert(out_point.clone());
                    Ok(())
                }
                Some(false) => Err(OutPointError::Dead(out_point.clone())),
                None => Err(OutPointError::Unknown(out_point.clone())),
            }
        };

        // // check input
        for cell_meta in &self.resolved_inputs {
            check_cell(&cell_meta.out_point)?;
        }

        let mut resolved_system_deps: HashSet<&OutPoint> = HashSet::new();
        if let Some(system_cell) = SYSTEM_CELL.get() {
            for cell_meta in &self.resolved_dep_groups {
                let cell_dep = CellDep::new_builder()
                    .out_point(cell_meta.out_point.clone())
                    .dep_type(DepType::DepGroup)
                    .build();

                let dep_group = system_cell.get(&cell_dep);
                if let Some(ResolvedDep::Group(_, cell_deps)) = dep_group {
                    resolved_system_deps.extend(cell_deps.iter().map(|dep| &dep.out_point));
                } else {
                    check_cell(&cell_meta.out_point)?;
                }
            }

            for cell_meta in &self.resolved_cell_deps {
                let cell_dep = CellDep::new_builder()
                    .out_point(cell_meta.out_point.clone())
                    .dep_type(DepType::Code)
                    .build();

                if system_cell.get(&cell_dep).is_none()
                    && !resolved_system_deps.contains(&cell_meta.out_point)
                {
                    check_cell(&cell_meta.out_point)?;
                }
            }
        } else {
            for cell_meta in self
                .resolved_cell_deps
                .iter()
                .chain(self.resolved_dep_groups.iter())
            {
                check_cell(&cell_meta.out_point)?;
            }
        }
```

**File:** util/types/src/core/cell.rs (L721-726)
```rust
        for out_point in transaction.input_pts_iter() {
            if !current_inputs.insert(out_point.to_owned()) {
                return Err(OutPointError::Dead(out_point));
            }
            resolved_inputs.push(resolve_cell(&out_point, false)?);
        }
```

**File:** util/types/src/core/cell.rs (L749-841)
```rust
fn resolve_transaction_deps_with_system_cell_cache<
    F: FnMut(&OutPoint, bool) -> Result<CellMeta, OutPointError>,
>(
    transaction: &TransactionView,
    cell_resolver: &mut F,
    resolved_cell_deps: &mut Vec<CellMeta>,
    resolved_dep_groups: &mut Vec<CellMeta>,
) -> Result<(), OutPointError> {
    // - If the dep expansion count of the transaction is not over the `MAX_DEP_EXPANSION_LIMIT`,
    //   it will always be accepted.
    // - If the dep expansion count of the transaction is over the `MAX_DEP_EXPANSION_LIMIT`, the
    //   behavior is as follow:
    //   | ckb v2021 | yes |             reject the transaction              |
    let mut remaining_dep_slots = MAX_DEP_EXPANSION_LIMIT;
    if let Some(system_cell) = SYSTEM_CELL.get() {
        for cell_dep in transaction.cell_deps_iter() {
            if let Some(resolved_dep) = system_cell.get(&cell_dep) {
                match resolved_dep {
                    ResolvedDep::Cell(cell_meta) => {
                        resolved_cell_deps.push(cell_meta.clone());
                        remaining_dep_slots = remaining_dep_slots
                            .checked_sub(1)
                            .ok_or(OutPointError::OverMaxDepExpansionLimit)?;
                    }
                    ResolvedDep::Group(dep_group, cell_deps) => {
                        resolved_dep_groups.push(dep_group.clone());
                        resolved_cell_deps.extend(cell_deps.clone());
                        remaining_dep_slots = remaining_dep_slots
                            .checked_sub(cell_deps.len())
                            .ok_or(OutPointError::OverMaxDepExpansionLimit)?;
                    }
                }
            } else {
                resolve_transaction_dep(
                    &cell_dep,
                    cell_resolver,
                    resolved_cell_deps,
                    resolved_dep_groups,
                    false, // don't eager_load data
                    &mut remaining_dep_slots,
                )?;
            }
        }
    } else {
        for cell_dep in transaction.cell_deps_iter() {
            resolve_transaction_dep(
                &cell_dep,
                cell_resolver,
                resolved_cell_deps,
                resolved_dep_groups,
                false, // don't eager_load data
                &mut remaining_dep_slots,
            )?;
        }
    }
    Ok(())
}

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
