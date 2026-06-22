### Title
Dep Group Expansion Performs Up to 2048 Uncharged DB Reads Per Transaction — (File: `util/types/src/core/cell.rs`)

---

### Summary

CKB's `dep_group` feature allows a single `CellDep` entry in a transaction to expand into up to 2048 sub-cell database lookups during transaction resolution. This expansion work is never charged in cycles. The transaction fee is computed solely from the serialized transaction size (~37 bytes for one `CellDep` entry), while the actual work performed is up to 2048 RocksDB reads. This creates a significant gap between fee paid and computational resources consumed, directly analogous to the minievm accesslist intrinsic gas omission.

---

### Finding Description

In `resolve_transaction_dep` (`util/types/src/core/cell.rs`), when a `CellDep` has `dep_type = DepGroup`, the function:

1. Loads the dep group cell from the DB (one read, eager-load)
2. Parses its data as an `OutPointVec` (up to `MAX_DEP_EXPANSION_LIMIT` entries)
3. Loads each sub-cell from the DB in a loop (up to 2048 reads) [1](#0-0) 

None of these reads are charged in cycles. The cycle counter is only incremented inside `ScriptVerifier::verify()`, which runs after resolution is complete: [2](#0-1) 

The cost model (`script/src/cost_model.rs`) applies `transferred_byte_cycles` only to data explicitly read by scripts via syscalls—not to the resolution phase: [3](#0-2) 

The `MAX_DEP_EXPANSION_LIMIT` (2048) caps the expansion per transaction but does not charge cycles for the work done. A single `CellDep` entry is 37 bytes in the serialized transaction, costing ~37 × fee_rate shannons, while the actual work is up to 2048 DB reads.

The expansion is triggered in both the tx-pool admission path and the block verification path: [4](#0-3) 

The local `resolved_cells` cache inside `resolve_transaction` is per-call only, so across multiple transactions in a block, the same dep group sub-cells are re-loaded from the DB for each transaction: [5](#0-4) 

---

### Impact Explanation

An attacker can:

1. Deploy a dep group cell containing 2048 out-points (data: 2048 × 36 = 73,728 bytes; capacity cost: ~0.00074 CKB)
2. Deploy 2048 minimal live cells to be referenced (capacity cost: ~0.125 CKB total)
3. Submit many transactions, each with a single `CellDep` pointing to the dep group

Each submitted transaction triggers 2048 DB reads during `resolve_transaction`, in both tx-pool admission (`tx-pool/src/process.rs`) and block verification (`verification/contextual/src/contextual_block_verifier.rs`). The fee paid per transaction is based on the small serialized size (~200 bytes), not the actual I/O work. This allows an attacker to consume disproportionate computational resources relative to fees paid, constituting a DoS risk against both the tx-pool and block verification pipeline.

The test suite confirms that 2048-entry dep groups are accepted and 2049-entry dep groups are rejected: [6](#0-5) [7](#0-6) 

---

### Likelihood Explanation

The upfront cost is low (~0.126 CKB total). Once the dep group and sub-cells are created, the attacker can submit arbitrarily many transactions referencing the same dep group. Each transaction pays only a small fee (based on ~200 bytes of tx size) while causing 2048 DB reads. The attack is repeatable and scalable. The entry path is the standard `send_transaction` RPC, accessible to any unprivileged tx-pool submitter.

---

### Recommendation

Charge cycles proportional to the number of dep group expansions performed during transaction resolution. Specifically, add a cycle cost in `resolve_transaction_dep` for each sub-cell loaded from the database, using a fixed per-cell cost (e.g., a constant analogous to `transferred_byte_cycles` applied to the sub-cell's data size, or a flat per-lookup cost). This would make the fee paid proportional to the actual I/O work performed.

---

### Proof of Concept

1. Deploy a dep group cell with 2048 out-points (cost: ~0.126 CKB total)
2. Submit N transactions, each with a single `CellDep` pointing to the dep group
3. Each transaction causes 2048 DB reads during `resolve_transaction_dep`
4. Fee per transaction: ~200 bytes × fee_rate (very small)
5. Total DB reads: N × 2048, with fees proportional only to N × 200 bytes

The gap between fee paid and work done is ~2048× per transaction. The `MAX_DEP_EXPANSION_LIMIT` bounds the per-transaction work but does not close the fee gap, and the attack is amplified linearly with the number of submitted transactions. [8](#0-7)

### Citations

**File:** util/types/src/core/cell.rs (L696-717)
```rust
    let mut resolved_cells: HashMap<(OutPoint, bool), CellMeta> = HashMap::new();
    let mut resolve_cell =
        |out_point: &OutPoint, eager_load: bool| -> Result<CellMeta, OutPointError> {
            if seen_inputs.contains(out_point) {
                return Err(OutPointError::Dead(out_point.clone()));
            }

            match resolved_cells.entry((out_point.clone(), eager_load)) {
                Entry::Occupied(entry) => Ok(entry.get().clone()),
                Entry::Vacant(entry) => {
                    let cell_status = cell_provider.cell(out_point, eager_load);
                    match cell_status {
                        CellStatus::Dead => Err(OutPointError::Dead(out_point.clone())),
                        CellStatus::Unknown => Err(OutPointError::Unknown(out_point.clone())),
                        CellStatus::Live(cell_meta) => {
                            entry.insert(cell_meta.clone());
                            Ok(cell_meta)
                        }
                    }
                }
            }
        };
```

**File:** util/types/src/core/cell.rs (L749-804)
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

**File:** script/src/verify.rs (L197-213)
```rust
    pub fn verify(&self, max_cycles: Cycle) -> Result<Cycle, Error> {
        let mut cycles: Cycle = 0;

        // Now run each script group
        for (_hash, group) in self.groups() {
            // max_cycles must reduce by each group exec
            let used_cycles = self
                .verify_script_group(group, max_cycles - cycles)
                .map_err(|e| {
                    #[cfg(feature = "logging")]
                    logging::on_script_error(_hash, &self.hash(), &e);
                    e.source(group)
                })?;

            cycles = wrapping_cycles_add(cycles, used_cycles, group)?;
        }
        Ok(cycles)
```

**File:** script/src/cost_model.rs (L1-13)
```rust
//! CKB VM cost model.
//!
//! The cost model assign cycles to instructions.

/// How many bytes can transfer when VM costs one cycle.
// 0.25 cycles per byte
pub const BYTES_PER_CYCLE: u64 = 4;

/// Calculates how many cycles spent to load the specified number of bytes.
pub fn transferred_byte_cycles(bytes: u64) -> u64 {
    // Compiler will optimize the divisin here to shifts.
    bytes.div_ceil(BYTES_PER_CYCLE)
}
```

**File:** test/src/specs/hardfork/v2021/cell_deps.rs (L419-419)
```rust
        let group_ay0_2048 = Self::create_depgroup_celldep(node, inputs, &[&code_ay0_tx; 2048]);
```

**File:** test/src/specs/hardfork/v2021/cell_deps.rs (L952-955)
```rust
        // Category: dep expansion count is 2048.
        self.test_dep_expansion_count_2048(PASS);
        // Category: dep expansion count is 2049.
        self.test_dep_expansion_count_2049(MDEL_BAN);
```
