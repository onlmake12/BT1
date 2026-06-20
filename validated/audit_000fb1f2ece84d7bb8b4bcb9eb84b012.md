### Title
Missing Duplicate-Input Check in `NonContextualTransactionVerifier` Enables Capacity Duplication — (`verification/src/transaction_verifier.rs`)

---

### Summary

`NonContextualTransactionVerifier` explicitly guards against duplicate cell-deps and header-deps via `DuplicateDepsVerifier`, but contains **no analogous guard for duplicate transaction inputs**. A miner who bypasses the tx-pool can include a transaction whose input list references the same `OutPoint` twice. Because `CapacityVerifier` sums the capacity of every entry in `resolved_inputs`, the duplicated cell's capacity is counted twice, allowing outputs to exceed the true on-chain capacity of the consumed cell — creating CKB capacity out of thin air.

---

### Finding Description

`NonContextualTransactionVerifier` is composed of exactly these sub-verifiers:

```
version | size | empty | duplicate_deps | outputs_data_verifier | script_hash_type
``` [1](#0-0) 

`DuplicateDepsVerifier::verify` iterates `cell_deps_iter()` and `header_deps_iter()` and rejects any duplicate, but **inputs are never iterated for uniqueness**: [2](#0-1) 

`CapacityVerifier::verify` then computes `inputs_sum` by summing the capacity of every element in `resolved_inputs`: [3](#0-2) 

If the same `OutPoint` appears twice in a transaction's input list, the cell resolver resolves it twice (the cell is still live at resolution time — it is only marked dead when the block is applied), producing two identical `CellMeta` entries in `resolved_inputs`. `inputs_capacity()` folds over that slice without deduplication, so the capacity of the single live cell is counted twice. The attacker can therefore set `outputs_sum` to up to `2 × cell_capacity` and pass the `inputs_sum < outputs_sum` guard.

The tx-pool does catch this case: `record_entry_edges` calls `edges.insert_input` for every input, and the second call for the same `OutPoint` returns `Reject::RBFRejected`: [4](#0-3) 

However, this is a **tx-pool admission check only**. A miner assembling a block template directly (e.g., via the `get_block_template` / custom block assembly path) is not constrained by tx-pool admission. The consensus-level verifier — `NonContextualTransactionVerifier` — has no equivalent guard, so the crafted transaction passes all consensus checks and the block is accepted by every full node.

---

### Impact Explanation

Any miner can craft a transaction spending a single cell twice within the same transaction, doubling the apparent input capacity. Outputs can then carry up to twice the capacity of the consumed cell. Repeated across multiple blocks or multiple inputs, this allows unbounded on-chain CKB issuance beyond the protocol's hard cap, breaking the capacity accounting invariant that `sum(outputs) ≤ sum(inputs)` for non-cellbase, non-DAO transactions.

---

### Likelihood Explanation

No privileged key, no majority hashpower, and no social engineering are required. Any solo miner who wins a single block can include the malicious transaction. The tx-pool barrier is trivially bypassed by constructing the block template manually. The attack is deterministic and repeatable every time the attacker mines a block.

---

### Recommendation

Add a `DuplicateInputsVerifier` to `NonContextualTransactionVerifier` (alongside `DuplicateDepsVerifier`) that iterates `transaction.input_pts_iter()`, inserts each `OutPoint` into a `HashSet`, and returns `TransactionError::DuplicateInputs` on the first collision. This mirrors the existing pattern for cell-deps: [5](#0-4) 

---

### Proof of Concept

1. Identify a live cell `C` with capacity `K` CKB owned by the attacker's lock script.
2. Construct a transaction `T`:
   - `inputs = [OutPoint(C), OutPoint(C)]` — the same out-point twice.
   - `outputs = [CellOutput { capacity: 2K - fee, lock: attacker_lock }]`
3. Sign both inputs with the attacker's key (the lock script executes twice, both times successfully, because it only checks the signature, not input uniqueness).
4. Assemble a block template that includes `T` directly, bypassing the tx-pool.
5. Mine and broadcast the block.
6. `NonContextualTransactionVerifier` passes (no duplicate-input check).
7. `CapacityVerifier` computes `inputs_sum = K + K = 2K`, `outputs_sum = 2K - fee`; the check `inputs_sum < outputs_sum` is false → passes.
8. The block is accepted by all full nodes. The attacker now holds a cell with `2K - fee` capacity while only one cell of capacity `K` was consumed on-chain. [1](#0-0) [6](#0-5)

### Citations

**File:** verification/src/transaction_verifier.rs (L71-103)
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
}
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

**File:** verification/src/transaction_verifier.rs (L461-515)
```rust
/// Perform inputs and outputs `capacity` field related verification
pub struct CapacityVerifier {
    resolved_transaction: Arc<ResolvedTransaction>,
    dao_type_hash: Byte32,
}

impl CapacityVerifier {
    /// Create a new `CapacityVerifier`
    pub fn new(resolved_transaction: Arc<ResolvedTransaction>, dao_type_hash: Byte32) -> Self {
        CapacityVerifier {
            resolved_transaction,
            dao_type_hash,
        }
    }

    /// Verify sum of inputs capacity should be greater than or equal to sum of outputs capacity
    /// Verify outputs capacity should be greater than or equal to its occupied capacity
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

        for (index, (output, data)) in self
            .resolved_transaction
            .transaction
            .outputs_with_data_iter()
            .enumerate()
        {
            let data_occupied_capacity = Capacity::bytes(data.len())?;
            if output.is_lack_of_capacity(data_occupied_capacity)? {
                return Err((TransactionError::InsufficientCellCapacity {
                    index,
                    inner: TransactionErrorSource::Outputs,
                    capacity: output.capacity().into(),
                    occupied_capacity: output.occupied_capacity(data_occupied_capacity)?,
                })
                .into());
            }
        }

        Ok(())
    }
```

**File:** tx-pool/src/component/edges.rs (L33-54)
```rust
    pub(crate) fn insert_input(
        &mut self,
        out_point: OutPoint,
        txid: ProposalShortId,
    ) -> Result<(), Reject> {
        // inputs is occupied means double speanding happened here
        match self.inputs.entry(out_point.clone()) {
            Entry::Occupied(occupied) => {
                let msg = format!(
                    "txpool unexpected double-spending out_point: {:?} old_tx: {:?} new_tx: {:?}",
                    out_point,
                    occupied.get(),
                    txid
                );
                Err(Reject::RBFRejected(msg))
            }
            Entry::Vacant(vacant) => {
                vacant.insert(txid);
                Ok(())
            }
        }
    }
```
