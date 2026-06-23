### Title
`DaoScriptSizeVerifier` Bypassed via Index Mismatch Between DAO Deposit Input and Withdrawing Output — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`DaoScriptSizeVerifier::verify()` enforces that a Nervos DAO deposit cell and its corresponding withdrawing cell use lock scripts of the same byte size. This guard compensates for a known bug in the on-chain DAO C script, which uses the **withdrawing** cell's lock script size (not the deposit cell's) when computing occupied capacity during Phase 2 withdrawal. However, the verifier pairs inputs with outputs strictly by position using `zip`, so if the DAO deposit input and the DAO withdrawing output appear at **different indices** in the same transaction, the size check is silently skipped. An attacker can exploit this to create a withdrawing cell with a smaller lock script than the deposit cell, causing the DAO C script to compute a larger `counted_capacity` and thus pay out more CKB than was deposited.

---

### Finding Description

`DaoScriptSizeVerifier::verify()` iterates over `(resolved_inputs[i], outputs[i])` pairs produced by `zip`:

```rust
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
        continue;                          // ← skips if either side is not DAO
    }
    ...
    if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
        return Err((TransactionError::DaoLockSizeMismatch { index: i }).into());
    }
}
``` [1](#0-0) 

The guard only fires when **both** the input at position `i` **and** the output at position `i` carry the DAO type script. An attacker can trivially defeat this by constructing a Phase 1 (prepare) transaction whose inputs and outputs are ordered so the DAO deposit input and the DAO withdrawing output are never at the same index:

| Index | Input | Output |
|-------|-------|--------|
| 0 | fee cell (non-DAO) | withdrawing cell (DAO, **small** lock script) |
| 1 | deposit cell (DAO, **large** lock script, data = `0x0000000000000000`) | change cell (non-DAO) |

- Pair `(input[0]=non-DAO, output[0]=DAO)` → `cell_uses_dao_type_script` fails for input → `continue`
- Pair `(input[1]=DAO deposit, output[1]=non-DAO)` → `cell_uses_dao_type_script` fails for output → `continue`

The size mismatch is never detected. The transaction passes both tx-pool admission [2](#0-1) 

and block-level verification [3](#0-2) 

The DAO C script itself does not enforce lock script size equality between deposit and withdrawing cells — that is precisely why `DaoScriptSizeVerifier` was introduced as a "temporary solution": [4](#0-3) 

With the guard bypassed, the C script proceeds to Phase 2 using the **withdrawing** cell's (smaller) lock script size to compute `occupied_capacity`, inflating `counted_capacity` and thus the final withdrawal amount.

The capacity arithmetic in `DaoCalculator::calculate_maximum_withdraw` confirms the impact:

```rust
let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [5](#0-4) 

`output` here is the withdrawing cell being spent. A smaller lock script → smaller `occupied_capacity` → larger `counted_capacity` → larger `withdraw_counted_capacity` → attacker receives more CKB than deposited.

---

### Impact Explanation

An attacker who deposits CKB with a large lock script (e.g., a multisig script with many keys, ~100 bytes) and then creates a Phase 1 transaction with a minimal lock script (~20 bytes) in the withdrawing output — while placing the cells at mismatched indices — will receive extra CKB on withdrawal proportional to `(lock_size_diff_bytes) × (withdrawing_ar / deposit_ar)`. For a 1,000,000 CKB deposit with an 80-byte lock script size difference and a typical DAO interest rate, this represents a meaningful theft of other depositors' funds from the DAO contract.

---

### Likelihood Explanation

The attack requires only:
1. A standard DAO deposit transaction (no special privilege).
2. A Phase 1 (prepare) transaction with inputs and outputs reordered — a capability any transaction sender has.
3. A Phase 2 (withdraw) transaction after the lock period.

No trusted role, key leak, or majority hashpower is needed. The transaction is submitted via the standard RPC (`send_transaction`) and is accepted by the tx-pool because `DaoScriptSizeVerifier` silently passes.

---

### Recommendation

Replace the index-based `zip` pairing with an explicit search that matches each DAO deposit input to its corresponding DAO withdrawing output regardless of position. Concretely, for every input cell that is a DAO deposit (type script matches, data is all zeros, block number ≥ `starting_block_limiting_dao_withdrawing_lock`), scan **all** outputs for a DAO-typed output and verify its lock script size matches. Alternatively, require that DAO deposit inputs and their corresponding withdrawing outputs always occupy the same index (enforced as a consensus rule), and reject any transaction that violates this ordering.

---

### Proof of Concept

Construct a Phase 1 transaction as follows (pseudocode):

```rust
// inputs[0] = fee cell (non-DAO lock, any data)
// inputs[1] = deposit cell (DAO type, data = [0u8;8], large lock script, e.g. 100-byte args)
// outputs[0] = withdrawing cell (DAO type, data = block_number_le, small lock script, e.g. 20-byte args)
// outputs[1] = change cell (non-DAO)

let tx = TransactionBuilder::default()
    .input(CellInput::new(fee_out_point, 0))
    .input(CellInput::new(deposit_out_point, 0))
    .output(
        CellOutput::new_builder()
            .capacity(deposit_capacity)
            .lock(small_lock_script)          // 20-byte args
            .type_(Some(dao_type_script))
            .build()
    )
    .output_data(block_number_le_bytes)       // marks as withdrawing cell
    .output(change_cell)
    .output_data(Bytes::default())
    .build();
```

- `DaoScriptSizeVerifier` pairs `(fee_cell, withdrawing_cell)` → input not DAO → skip; pairs `(deposit_cell, change_cell)` → output not DAO → skip.
- Transaction is accepted into the tx-pool and committed.
- In Phase 2, the DAO C script computes `occupied_capacity` using the 20-byte lock script, yielding a withdrawal larger than the correct amount computed from the 100-byte deposit lock script. [6](#0-5) [7](#0-6)

### Citations

**File:** verification/src/transaction_verifier.rs (L817-819)
```rust
/// Verifies that deposit cell and withdrawing cell in Nervos DAO use same sized lock scripts.
/// It provides a temporary solution till Nervos DAO script can be properly upgraded.
pub struct DaoScriptSizeVerifier<DL> {
```

**File:** verification/src/transaction_verifier.rs (L845-890)
```rust
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
```

**File:** tx-pool/src/util.rs (L85-132)
```rust
pub(crate) async fn verify_rtx(
    snapshot: Arc<Snapshot>,
    rtx: Arc<ResolvedTransaction>,
    tx_env: Arc<TxVerifyEnv>,
    cache_entry: &Option<CacheEntry>,
    max_tx_verify_cycles: Cycle,
    command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
) -> Result<Completed, Reject> {
    let consensus = snapshot.cloned_consensus();
    let data_loader = snapshot.as_data_loader();

    if let Some(completed) = cache_entry {
        TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
            .verify()
            .map(|_| *completed)
            .map_err(Reject::Verification)
    } else if let Some(command_rx) = command_rx {
        ContextualTransactionVerifier::new(
            Arc::clone(&rtx),
            consensus,
            data_loader,
            Arc::clone(&tx_env),
        )
        .verify_with_pause(max_tx_verify_cycles, command_rx)
        .await
        .and_then(|result| {
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
                })
                .map_err(Reject::Verification)
        })
    }
}
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L28-31)
```rust
use ckb_verification::{
    BlockErrorKind, CellbaseError, CommitError, ContextualTransactionVerifier,
    DaoScriptSizeVerifier, TimeRelativeTransactionVerifier, UnknownParentError,
};
```

**File:** util/dao/src/lib.rs (L149-157)
```rust
        let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
        let output_capacity: Capacity = output.capacity().into();
        let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

```
