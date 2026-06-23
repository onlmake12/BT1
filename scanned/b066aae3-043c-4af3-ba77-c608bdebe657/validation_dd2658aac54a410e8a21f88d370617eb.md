### Title
Missing Duplicate Transaction Input Check Allows Capacity Inflation — (`verification/src/transaction_verifier.rs`, `util/types/src/core/cell.rs`)

---

### Summary

`NonContextualTransactionVerifier` explicitly checks for duplicate `cell_deps` and `header_deps` via `DuplicateDepsVerifier`, but contains no analogous check for duplicate transaction **inputs**. The `ResolvedTransaction::check` function also fails to detect intra-transaction duplicate inputs because `seen_inputs` is only extended after all inputs are processed. As a result, a transaction containing the same `OutPoint` multiple times in its inputs list passes all verification stages, and `inputs_capacity()` counts the same cell's capacity once per duplicate occurrence, enabling outputs to exceed the actual available capacity.

---

### Finding Description

`NonContextualTransactionVerifier` is the primary context-independent gate for all submitted transactions. It runs six sub-verifiers: [1](#0-0) 

`DuplicateDepsVerifier` correctly rejects duplicate `cell_deps` and `header_deps`: [2](#0-1) 

However, there is **no `DuplicateInputsVerifier`** and no equivalent check for duplicate entries in the `inputs` list. A grep for any form of `DuplicateInput` or `duplicate.*input` returns zero matches across the entire codebase.

The second gate, `ResolvedTransaction::check`, also fails to catch intra-transaction duplicates. The `seen_inputs` set is checked at the start of each input but is only extended **after** the entire input loop completes: [3](#0-2) [4](#0-3) 

For a transaction with inputs `[A, A]`:
- First iteration: `seen_inputs.contains(A)` → `false` → passes
- Second iteration: `seen_inputs.contains(A)` → still `false` (not yet inserted) → passes
- Line 382: `seen_inputs.extend([A, A])` → only one `A` in the set

Both occurrences resolve to the same live `CellMeta`, so `resolved_inputs` contains two identical entries. `inputs_capacity()` then sums them both: [5](#0-4) 

`CapacityVerifier` compares this inflated sum against `outputs_sum`: [6](#0-5) 

---

### Impact Explanation

An attacker who controls a cell with capacity `C` CKB can include its `OutPoint` `N` times in a single transaction's inputs. `inputs_capacity()` returns `N × C`. The `CapacityVerifier` then permits outputs totalling up to `N × C` CKB. Only `C` CKB of real value backs those outputs. This is a **CKB inflation / capacity counterfeiting** vulnerability: the attacker mints `(N-1) × C` CKB from nothing, directly violating the cell model's conservation invariant and the total CKB supply cap.

---

### Likelihood Explanation

The attack requires only the ability to submit a transaction via the standard `send_transaction` RPC or the P2P relay path — both are open to any unprivileged user. No special keys, hashpower, or privileged access are needed. The attacker only needs to own one live cell to exploit this. The crafted transaction is structurally valid (correct serialization, valid lock script witness for each occurrence of the duplicated input), so it passes all other checks.

---

### Recommendation

Add a `DuplicateInputsVerifier` to `NonContextualTransactionVerifier` in `verification/src/transaction_verifier.rs`, mirroring the existing `DuplicateDepsVerifier`:

```rust
let mut seen = HashSet::with_capacity(tx.inputs().len());
for input in tx.inputs_iter() {
    if !seen.insert(input.previous_output()) {
        return Err(TransactionError::DuplicateInputs {
            out_point: input.previous_output(),
        }.into());
    }
}
```

This check should be added to `NonContextualTransactionVerifier::verify()` alongside `duplicate_deps`, so it is enforced at the earliest possible stage — before resolution and before script execution. [7](#0-6) 

---

### Proof of Concept

1. Attacker owns cell `X` at `OutPoint { tx_hash: H, index: 0 }` with capacity 100 CKB.
2. Attacker constructs a transaction:
   - `inputs`: `[CellInput { previous_output: (H, 0) }, CellInput { previous_output: (H, 0) }]`
   - `outputs`: one cell with capacity 200 CKB
   - `witnesses`: two valid signatures (or `always_success` lock)
3. `NonContextualTransactionVerifier::verify()` passes — no duplicate input check exists.
4. `resolve_transaction` resolves both inputs to the same live `CellMeta` for cell `X`.
5. `ResolvedTransaction::check` passes — `seen_inputs` is empty for both iterations; `X` is inserted only after the loop.
6. `inputs_capacity()` = 100 + 100 = 200 CKB.
7. `CapacityVerifier`: 200 ≥ 200 → passes.
8. Script verifier runs the lock script twice; both executions pass.
9. Transaction commits. Attacker now holds a 200 CKB cell, having spent only 100 CKB. [1](#0-0) [8](#0-7) [5](#0-4)

### Citations

**File:** verification/src/transaction_verifier.rs (L71-102)
```rust
pub struct NonContextualTransactionVerifier<'a> {
    pub(crate) version: VersionVerifier<'a>,
    pub(crate) size: SizeVerifier<'a>,
    pub(crate) empty: EmptyVerifier<'a>,
    pub(crate) duplicate_deps: DuplicateDepsVerifier<'a>,
    pub(crate) outputs_data_verifier: OutputsDataVerifier<'a>,
    pub(crate) script_hash_type: ScriptHashTypeVerifier<'a>,
}

impl<'a> NonContextualTransactionVerifier<'a> {
    /// Creates a new NonContextualTransactionVerifier
    pub fn new(tx: &'a TransactionView, consensus: &'a Consensus) -> Self {
        NonContextualTransactionVerifier {
            version: VersionVerifier::new(tx, consensus.tx_version()),
            size: SizeVerifier::new(tx, consensus.max_block_bytes()),
            empty: EmptyVerifier::new(tx),
            duplicate_deps: DuplicateDepsVerifier::new(tx),
            outputs_data_verifier: OutputsDataVerifier::new(tx),
            script_hash_type: ScriptHashTypeVerifier::new(tx),
        }
    }

    /// Perform context-independent verification
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

**File:** verification/src/transaction_verifier.rs (L483-494)
```rust
        if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
            let inputs_sum = self.resolved_transaction.inputs_capacity()?;
            let outputs_sum = self.resolved_transaction.outputs_capacity()?;

            if inputs_sum < outputs_sum {
                return Err((TransactionError::OutputsSumOverflow {
                    inputs_sum,
                    outputs_sum,
                })
                .into());
            }
        }
```

**File:** util/types/src/core/cell.rs (L288-293)
```rust
    pub fn inputs_capacity(&self) -> CapacityResult<Capacity> {
        self.resolved_inputs
            .iter()
            .map(CellMeta::capacity)
            .try_fold(Capacity::zero(), Capacity::safe_add)
    }
```

**File:** util/types/src/core/cell.rs (L309-385)
```rust
    pub fn check<CC: CellChecker, HC: HeaderChecker, S: BuildHasher>(
        &self,
        seen_inputs: &mut HashSet<OutPoint, S>,
        cell_checker: &CC,
        header_checker: &HC,
    ) -> Result<(), OutPointError> {
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

        for block_hash in self.transaction.header_deps_iter() {
            header_checker.check_valid(&block_hash)?
        }

        seen_inputs.extend(self.resolved_inputs.iter().map(|i| &i.out_point).cloned());

        Ok(())
    }
```
