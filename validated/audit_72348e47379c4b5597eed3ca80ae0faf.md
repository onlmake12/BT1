### Title
Duplicate Transaction Inputs Bypass Capacity Accounting — (`util/types/src/core/cell.rs`, `verification/src/transaction_verifier.rs`)

---

### Summary

CKB's transaction verifier enforces uniqueness for `CellDep`s and `HeaderDep`s but has **no equivalent check for duplicate inputs**. When the same `OutPoint` appears twice in a transaction's input list, the liveness check in `ResolvedTransaction::check()` silently passes the second occurrence via a local `checked_cells` cache, and `inputs_capacity()` then double-counts that cell's capacity. This allows an unprivileged transaction sender to inflate the apparent input capacity and create outputs exceeding the actual CKB held in the consumed cells.

---

### Finding Description

**Vulnerability class:** Cell/capacity accounting — duplicate resource counting.

**Root cause 1 — No duplicate-inputs verifier:**

`NonContextualTransactionVerifier` runs six sub-verifiers on every transaction before it is admitted to the tx-pool or committed to a block: [1](#0-0) 

The sub-verifiers are `version`, `size`, `empty`, `duplicate_deps`, `outputs_data_verifier`, and `script_hash_type`. There is a `DuplicateDepsVerifier` that rejects repeated `CellDep` or `HeaderDep` entries: [2](#0-1) 

There is **no analogous `DuplicateInputsVerifier`**. The `TransactionError` enum defines `DuplicateCellDeps` and `DuplicateHeaderDeps` variants but no `DuplicateInputs` variant: [3](#0-2) 

**Root cause 2 — `check()` silently accepts the second occurrence of a duplicate input:**

`ResolvedTransaction::check()` iterates `resolved_inputs` and calls a `check_cell` closure for each entry. The closure maintains a local `checked_cells` set: [4](#0-3) 

When the same `OutPoint` appears twice in `resolved_inputs`:
- **First occurrence** — not in `seen_inputs` (cross-tx set), not in `checked_cells` → liveness confirmed → inserted into `checked_cells` → `Ok(())`.
- **Second occurrence** — not in `seen_inputs`, **IS** in `checked_cells` → returns `Ok(())` immediately, **without error**.

The `checked_cells` set was designed to avoid redundant liveness lookups when the same cell appears in both inputs and deps. It inadvertently suppresses the error that should fire when the same cell appears twice in inputs.

**Root cause 3 — `inputs_capacity()` sums without deduplication:** [5](#0-4) 

`resolved_inputs` is a plain `Vec<CellMeta>`. If the same cell was resolved twice (once per duplicate input), both entries are summed, inflating `inputs_sum`.

**Root cause 4 — `CapacityVerifier` trusts the inflated sum:** [6](#0-5) 

`inputs_sum` is compared against `outputs_sum`. With a doubled `inputs_sum`, the attacker can set outputs whose total capacity equals `2 × cell_capacity − fee`, passing the check even though only `cell_capacity` of real CKB was consumed.

---

### Impact Explanation

An attacker who controls a live cell of capacity `C` can craft a transaction with that cell's `OutPoint` listed twice in `inputs` and set outputs totalling `2C − fee`. The `CapacityVerifier` passes because it sees `inputs_sum = 2C ≥ outputs_sum = 2C − fee`. The attacker effectively mints `C − fee` CKB from nothing. If the block is accepted by the network, the inflated outputs become live cells, permanently corrupting the total CKB supply. This is a **consensus-breaking, token-inflation** vulnerability.

**Impact: High.**

---

### Likelihood Explanation

Any unprivileged user can submit a transaction via the `send_transaction` RPC. Crafting a transaction with a repeated input requires no special privilege, no key compromise, and no majority hashpower. The attacker only needs to own one live cell. The technique is straightforward for anyone familiar with CKB transaction structure.

**Likelihood: Low** (requires knowledge of the gap; not exploited by accident), but the barrier is purely informational.

---

### Recommendation

1. Add a `DuplicateInputs` variant to `TransactionError`.
2. Add a `DuplicateInputsVerifier` (mirroring `DuplicateDepsVerifier`) to `NonContextualTransactionVerifier` that iterates `transaction.inputs_iter()`, inserts each `previous_output()` into a `HashSet`, and returns `Err(TransactionError::DuplicateInputs { out_point })` on the first collision.
3. Alternatively (or additionally), fix `ResolvedTransaction::check()` to treat a duplicate entry in `resolved_inputs` as an error rather than a cache hit, by checking `checked_cells` before `seen_inputs` and returning `Err(OutPointError::Dead(...))` when the same out-point appears twice in the input list.

---

### Proof of Concept

```
1. Attacker owns live cell C with out_point OP and capacity = 100 CKB.
2. Attacker builds transaction T:
     inputs  = [CellInput(OP, since=0), CellInput(OP, since=0)]   ← same OP twice
     outputs = [CellOutput(capacity=199_9999_9999)]                ← 199.99... CKB
     witnesses = [valid unlock for C, valid unlock for C]
3. NonContextualTransactionVerifier passes (no DuplicateInputs check).
4. resolve_transaction resolves both inputs to CellMeta(OP, capacity=100 CKB),
   producing resolved_inputs = [CellMeta(100), CellMeta(100)].
5. ResolvedTransaction::check():
     - input[0]: OP not in seen_inputs, not in checked_cells → live → checked_cells={OP} → Ok
     - input[1]: OP not in seen_inputs, IS in checked_cells → Ok (silent pass)
6. inputs_capacity() = 100 + 100 = 200 CKB.
7. CapacityVerifier: 200 CKB >= 199.99... CKB → passes.
8. Lock script for C executes twice and succeeds (attacker controls it).
9. Block commits. Attacker now holds 199.99... CKB having spent only 100 CKB.
   Net gain: ~100 CKB minted from nothing.
```

### Citations

**File:** verification/src/transaction_verifier.rs (L71-101)
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
```

**File:** verification/src/transaction_verifier.rs (L428-458)
```rust
pub struct DuplicateDepsVerifier<'a> {
    transaction: &'a TransactionView,
}

impl<'a> DuplicateDepsVerifier<'a> {
    pub fn new(transaction: &'a TransactionView) -> Self {
        DuplicateDepsVerifier { transaction }
    }

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

**File:** verification/src/transaction_verifier.rs (L478-494)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        // skip OutputsSumOverflow verification for resolved cellbase and DAO
        // withdraw transactions.
        // cellbase's outputs are verified by RewardVerifier
        // DAO withdraw transaction is verified via the type script of DAO cells
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

**File:** util/types/src/core/error.rs (L115-131)
```rust
    /// There are duplicated [`CellDep`]s within the same transaction.
    ///
    /// [`CellDep`]: ../ckb_types/packed/struct.CellDep.html
    #[error("DuplicateCellDeps({out_point})")]
    DuplicateCellDeps {
        /// The out-point of that duplicated [`CellDep`].
        ///
        /// [`CellDep`]: ../ckb_types/packed/struct.CellDep.html
        out_point: OutPoint,
    },

    /// There are duplicated `HeaderDep` within the same transaction.
    #[error("DuplicateHeaderDeps({hash})")]
    DuplicateHeaderDeps {
        /// The block hash of that duplicated `HeaderDep.`
        hash: Byte32,
    },
```

**File:** util/types/src/core/cell.rs (L287-293)
```rust
    /// Returns the total capacity of all inputs.
    pub fn inputs_capacity(&self) -> CapacityResult<Capacity> {
        self.resolved_inputs
            .iter()
            .map(CellMeta::capacity)
            .try_fold(Capacity::zero(), Capacity::safe_add)
    }
```

**File:** util/types/src/core/cell.rs (L315-338)
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
```
