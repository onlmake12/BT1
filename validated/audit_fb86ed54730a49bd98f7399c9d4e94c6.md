### Title
Missing Duplicate-Input Check Allows Intra-Transaction Capacity Double-Counting — (File: `util/types/src/core/cell.rs`, `verification/src/transaction_verifier.rs`)

---

### Summary

`NonContextualTransactionVerifier` rejects duplicate `cell_deps` and `header_deps` but has no equivalent check for duplicate **inputs**. Because `inputs_capacity()` naively sums every entry in `resolved_inputs`, a transaction that lists the same `OutPoint` twice in its inputs vector will have its input capacity double-counted. The `CapacityVerifier` then accepts outputs whose total capacity exceeds the capacity of the single real cell being consumed, allowing an attacker to mint CKB from nothing.

---

### Finding Description

**Vulnerability class:** Cell/capacity accounting — iterative summation over an array that may contain duplicate entries, with no deduplication guard.

**Analog mapping:** The original report shows that iterating over an array of identical assets without updating intermediate state produces an undervalued delta-concentration and therefore a lower fee. In CKB the same structural flaw appears in capacity accounting: iterating over `resolved_inputs` without tracking within-transaction duplicates produces an over-valued `inputs_sum` and therefore a passing capacity check for outputs that exceed real input capacity.

**Root cause — three cooperating gaps:**

**1. `NonContextualTransactionVerifier` has no `DuplicateInputsVerifier`.**

`DuplicateDepsVerifier` is wired in for `cell_deps` and `header_deps`, but the inputs vector is never checked for duplicate `OutPoint`s at the non-contextual stage. [1](#0-0) 

`DuplicateDepsVerifier` itself only iterates `cell_deps_iter()` and `header_deps_iter()`: [2](#0-1) 

**2. `ResolvedTransaction::check()` only updates `seen_inputs` at the end of the inputs loop.**

`seen_inputs` is the cross-transaction double-spend guard. Within a single transaction the closure `check_cell` tests `seen_inputs.contains(out_point)` but `seen_inputs` is not extended until after **all** inputs have been iterated:

```rust
// check input
for cell_meta in &self.resolved_inputs {
    check_cell(&cell_meta.out_point)?;   // seen_inputs still empty for every entry
}
// ...
seen_inputs.extend(self.resolved_inputs.iter().map(|i| &i.out_point).cloned());
``` [3](#0-2) [4](#0-3) 

The `checked_cells` set inside the closure is only populated for **deps**, never for inputs, so a second occurrence of the same input `OutPoint` passes the liveness check identically to the first. [5](#0-4) 

**3. `inputs_capacity()` sums every entry in `resolved_inputs` without deduplication.**

```rust
pub fn inputs_capacity(&self) -> CapacityResult<Capacity> {
    self.resolved_inputs
        .iter()
        .map(CellMeta::capacity)
        .try_fold(Capacity::zero(), Capacity::safe_add)
}
``` [6](#0-5) 

`CapacityVerifier` calls this directly: [7](#0-6) 

---

### Impact Explanation

An attacker who lists the same live cell `OutPoint` N times in a transaction's inputs vector will have `inputs_capacity()` return N × (cell capacity). The `CapacityVerifier` compares this inflated sum against `outputs_capacity()`, so the attacker can create outputs whose total capacity is up to N × (cell capacity) while only one cell (with capacity C) is actually consumed. The net effect is (N−1) × C shannons of CKB created from nothing per transaction. This is a direct violation of the conservation-of-capacity invariant that underpins CKB's economic model.

**Impact: Critical** — arbitrary CKB inflation by any transaction sender.

---

### Likelihood Explanation

The attack requires only the ability to submit a transaction via the standard `send_transaction` RPC or to include a crafted transaction in a mined block. No privileged access, key material, or majority hash power is needed. The transaction structure is fully attacker-controlled, and the missing check is in the non-contextual (cheapest, first-pass) verifier, so the malformed transaction reaches the capacity check without being filtered earlier.

**Likelihood: High** — trivially reachable by any unprivileged RPC caller or miner.

---

### Recommendation

Add a `DuplicateInputsVerifier` (analogous to the existing `DuplicateDepsVerifier`) to `NonContextualTransactionVerifier`. It should iterate `transaction.input_pts_iter()`, insert each `OutPoint` into a `HashSet`, and return `TransactionError::DuplicateInputs` on the first collision. Wire it into `NonContextualTransactionVerifier::verify()` before the capacity check.

Alternatively, update `ResolvedTransaction::check()` to insert each input `OutPoint` into `seen_inputs` (or a local `checked_inputs` set) **immediately** after it is validated, mirroring the pattern used for `checked_cells` on deps, so that a second occurrence of the same `OutPoint` is rejected as `OutPointError::Dead`.

---

### Proof of Concept

```
Cell X: OutPoint = (tx_hash_A, 0), capacity = 100 CKB

Craft transaction T:
  inputs  = [ CellInput(X), CellInput(X) ]   // same OutPoint twice
  outputs = [ CellOutput(capacity = 190 CKB) ]

Verification path:
  NonContextualTransactionVerifier::verify()
    → DuplicateDepsVerifier: no cell_deps → passes
    → EmptyVerifier: inputs.len() == 2 → passes
    (no DuplicateInputsVerifier exists)

  resolve_transaction(T, &mut seen_inputs, ...)
    → resolves CellInput(X) → CellMeta{capacity=100} (live)
    → resolves CellInput(X) → CellMeta{capacity=100} (still live, seen_inputs not yet updated)
    → resolved_inputs = [CellMeta{100}, CellMeta{100}]

  ResolvedTransaction::check()
    → check_cell(X): seen_inputs empty → liveness OK
    → check_cell(X): seen_inputs still empty → liveness OK
    → seen_inputs.extend([X])   // too late

  CapacityVerifier::verify()
    → inputs_sum  = 100 + 100 = 200 CKB   (double-counted)
    → outputs_sum = 190 CKB
    → 200 >= 190 → PASSES

Result: 1 cell of 100 CKB consumed, 190 CKB output created → 90 CKB minted from nothing.
```

### Citations

**File:** verification/src/transaction_verifier.rs (L61-102)
```rust
/// Context-independent verification checks for transaction
///
/// Basic checks that don't depend on any context
/// Contains:
/// - Check for version
/// - Check for size
/// - Check inputs and output empty
/// - Check for duplicate deps
/// - Check for whether outputs match data
/// - Check whether output lock hash type within enabled range
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

**File:** verification/src/transaction_verifier.rs (L483-493)
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

**File:** util/types/src/core/cell.rs (L315-333)
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
```

**File:** util/types/src/core/cell.rs (L335-338)
```rust
        // // check input
        for cell_meta in &self.resolved_inputs {
            check_cell(&cell_meta.out_point)?;
        }
```

**File:** util/types/src/core/cell.rs (L382-382)
```rust
        seen_inputs.extend(self.resolved_inputs.iter().map(|i| &i.out_point).cloned());
```
