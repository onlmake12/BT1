### Title
Incorrect Index-Pairing Assumption in `DaoScriptSizeVerifier` Allows Lock-Script-Size Check Bypass — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`DaoScriptSizeVerifier::verify()` pairs transaction inputs with outputs using `.zip()` by positional index. It assumes that a DAO deposit input at position `i` always corresponds to the DAO withdrawing output at position `i`. No validation enforces this correspondence. An unprivileged transaction sender can place the DAO deposit input at index `i` and the DAO withdrawing output at index `j` (`j ≠ i`), causing the verifier to silently skip the lock-script-size check for the actual deposit→withdrawing pair. This allows the attacker to use a smaller lock script in the withdrawing cell, inflating the `counted_capacity` used in interest calculation and extracting more CKB than legitimately earned.

---

### Finding Description

`DaoScriptSizeVerifier::verify()` iterates over `(resolved_inputs[i], outputs[i])` pairs via `.zip()`:

```rust
// verification/src/transaction_verifier.rs  lines 847–888
for (i, (input_meta, cell_output)) in self
    .resolved_transaction
    .resolved_inputs
    .iter()
    .zip(self.resolved_transaction.transaction.outputs())
    .enumerate()
{
    if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
        && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
    {
        continue;                          // ← skips if either side lacks DAO type
    }
    // Only input data with full zeros are counted as deposit cell
    if input_data.into_iter().any(|b| b != 0) { continue; }
    ...
    if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
        return Err(...DaoLockSizeMismatch...);
    }
}
```

The check is only triggered when **both** `resolved_inputs[i]` and `outputs[i]` carry the DAO type script. There is no prior validation that DAO deposit inputs and DAO withdrawing outputs share the same positional index. A transaction sender can trivially violate this assumption:

| Index | Input | Output |
|-------|-------|--------|
| 0 | DAO deposit cell (large lock, all-zero data) | non-DAO cell |
| 1 | non-DAO cell | DAO withdrawing cell (small lock) |

The `.zip()` produces pairs `(deposit, non-DAO)` and `(non-DAO, withdrawing)`. Both pairs are skipped by the `cell_uses_dao_type_script` guard. The actual mismatched pair `(deposit[0], withdrawing[1])` is never evaluated.

The comment in the source explicitly acknowledges this verifier is the **only** enforcement layer: *"It provides a temporary solution till Nervos DAO script can be properly upgraded."* [1](#0-0) 

The interest calculation in `DaoCalculator::calculate_maximum_withdraw` shows why a smaller lock script in the withdrawing cell is profitable:

```rust
// util/dao/src/lib.rs  lines 149–156
let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
let output_capacity: Capacity = output.capacity().into();
let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

`occupied_capacity` is derived from the withdrawing cell's lock script size. A smaller lock script → smaller `occupied_capacity` → larger `counted_capacity` → larger interest payout. The `occupied_capacity` is then added back, so the attacker recovers both the inflated interest and the full occupied capacity. [2](#0-1) 

---

### Impact Explanation

An attacker who has deposited CKB into the Nervos DAO with a large lock script can, during Phase 1 withdrawal (deposit → withdrawing), submit a transaction where the deposit input and the withdrawing output are at **different** positional indices. The `DaoScriptSizeVerifier` silently skips the size check. The attacker places a smaller lock script on the withdrawing cell, reducing `occupied_capacity` and inflating the interest-bearing `counted_capacity`. In Phase 2, `calculate_maximum_withdraw` computes a higher payout than the deposit entitles. The attacker extracts more CKB than they deposited, at the expense of the DAO interest pool shared by all depositors. [3](#0-2) [4](#0-3) 

---

### Likelihood Explanation

The entry path is fully unprivileged: any account that has deposited CKB into the DAO can submit a crafted Phase 1 withdrawal transaction via the standard `send_transaction` RPC. No special role, key, or majority hash power is required. The only prerequisite is an existing DAO deposit. The bypass requires only reordering inputs and outputs in the transaction, which is a normal capability of any transaction builder. `DaoScriptSizeVerifier` is invoked in both the tx-pool admission path (`tx-pool/src/util.rs`) and the contextual block verifier (`verification/contextual/src/contextual_block_verifier.rs`), so the bypass affects both paths. [5](#0-4) 

---

### Recommendation

Replace the positional `.zip()` pairing with an explicit matching strategy. For each DAO deposit input (all-zero data, DAO type script), find the corresponding DAO withdrawing output by the out-point stored in the withdrawing cell's data or by requiring that the transaction explicitly declares the pairing (e.g., via a witness field). Alternatively, enforce that all DAO deposit inputs and their corresponding DAO withdrawing outputs occupy the same positional index by adding a pre-check that rejects any transaction where this invariant is violated. The fix is analogous to the remediation in the referenced report: add a validation step before the main loop that asserts the structural invariant that the verifier relies on. [6](#0-5) 

---

### Proof of Concept

Construct a Phase 1 DAO withdrawal transaction with the following layout:

```
resolved_inputs[0]  = DAO deposit cell
                      lock: Script { args: Bytes::from(vec![0u8; 100]) }   // 100-byte args
                      type: DAO type script
                      data: [0u8; 8]   // all-zero → deposit cell
                      capacity: 1_000_000 CKB

resolved_inputs[1]  = any non-DAO cell (e.g., a plain CKB cell)

outputs[0]          = any non-DAO cell

outputs[1]          = DAO withdrawing cell
                      lock: Script { args: Bytes::new() }                  // 0-byte args
                      type: DAO type script
                      data: <deposit block number as u64 LE>
                      capacity: 1_000_000 CKB
```

`DaoScriptSizeVerifier::verify()` evaluates:
- Pair `(inputs[0], outputs[0])`: DAO input + non-DAO output → `cell_uses_dao_type_script` fails for output → **skipped**
- Pair `(inputs[1], outputs[1])`: non-DAO input + DAO output → `cell_uses_dao_type_script` fails for input → **skipped**

The lock-script-size mismatch (100-byte args vs 0-byte args) is never detected. In Phase 2, `calculate_maximum_withdraw` uses the withdrawing cell's 0-byte-args lock script, yielding a smaller `occupied_capacity` and a larger `counted_capacity`, producing a higher interest payout than the deposit's 100-byte-args lock script would have entitled. [7](#0-6) [2](#0-1)

### Citations

**File:** verification/src/transaction_verifier.rs (L817-891)
```rust
/// Verifies that deposit cell and withdrawing cell in Nervos DAO use same sized lock scripts.
/// It provides a temporary solution till Nervos DAO script can be properly upgraded.
pub struct DaoScriptSizeVerifier<DL> {
    resolved_transaction: Arc<ResolvedTransaction>,
    consensus: Arc<Consensus>,
    data_loader: DL,
}

impl<DL: CellDataProvider> DaoScriptSizeVerifier<DL> {
    /// Create a new `DaoScriptSizeVerifier`
    pub fn new(
        resolved_transaction: Arc<ResolvedTransaction>,
        consensus: Arc<Consensus>,
        data_loader: DL,
    ) -> Self {
        DaoScriptSizeVerifier {
            resolved_transaction,
            consensus,
            data_loader,
        }
    }

    fn dao_type_hash(&self) -> Byte32 {
        self.consensus.dao_type_hash()
    }

    /// Verifies that for all Nervos DAO transactions, withdrawing cells must use lock scripts
    /// of the same size as corresponding deposit cells
    pub fn verify(&self) -> Result<(), Error> {
        let dao_type_hash = self.dao_type_hash();
        for (i, (input_meta, cell_output)) in self
            .resolved_transaction
            .resolved_inputs
            .iter()
            .zip(self.resolved_transaction.transaction.outputs())
            .enumerate()
        {
            // Both the input and output cell must use Nervos DAO as type script
            if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
                && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
            {
                continue;
            }

            // A Nervos DAO deposit cell must have input data
            let input_data = match self.data_loader.load_cell_data(input_meta) {
                Some(data) => data,
                None => continue,
            };

            // Only input data with full zeros are counted as deposit cell
            if input_data.into_iter().any(|b| b != 0) {
                continue;
            }

            // Only cells committed after the pre-defined block number in consensus is
            // applied to this rule
            if let Some(info) = &input_meta.transaction_info
                && info.block_number
                    < self
                        .consensus
                        .starting_block_limiting_dao_withdrawing_lock()
            {
                continue;
            }

            // Now we have a pair of DAO deposit and withdrawing cells, it is expected
            // they have the lock scripts of the same size.
            if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
                return Err((TransactionError::DaoLockSizeMismatch { index: i }).into());
            }
        }
        Ok(())
    }
}
```

**File:** util/dao/src/lib.rs (L126-158)
```rust
    /// Calculate maximum withdraw capacity of a deposited dao output
    pub fn calculate_maximum_withdraw(
        &self,
        output: &CellOutput,
        output_data_capacity: Capacity,
        deposit_header_hash: &Byte32,
        withdrawing_header_hash: &Byte32,
    ) -> Result<Capacity, DaoError> {
        let deposit_header = self
            .data_loader
            .get_header(deposit_header_hash)
            .ok_or(DaoError::InvalidHeader)?;
        let withdrawing_header = self
            .data_loader
            .get_header(withdrawing_header_hash)
            .ok_or(DaoError::InvalidHeader)?;
        if deposit_header.number() >= withdrawing_header.number() {
            return Err(DaoError::InvalidOutPoint);
        }

        let (deposit_ar, _, _, _) = extract_dao_data(deposit_header.dao());
        let (withdrawing_ar, _, _, _) = extract_dao_data(withdrawing_header.dao());

        let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
        let output_capacity: Capacity = output.capacity().into();
        let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

        Ok(withdraw_capacity)
```

**File:** tx-pool/src/util.rs (L111-127)
```rust
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
    } else {
        block_in_place(|| {
            ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
                .verify(max_tx_verify_cycles, false)
                .and_then(|result| {
                    DaoScriptSizeVerifier::new(
                        rtx,
                        snapshot.cloned_consensus(),
                        snapshot.as_data_loader(),
                    )
                    .verify()?;
                    Ok(result)
```
