### Title
`DaoScriptSizeVerifier` Positional-Zip Bypass Allows Inflated NervosDAO Interest Extraction — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`DaoScriptSizeVerifier` enforces that a DAO deposit cell and its corresponding withdrawal-preparation output share the same lock-script size. It does so by zipping `resolved_inputs` with `outputs` positionally. An unprivileged transaction sender can defeat this check by inserting a non-DAO input at index 0 before the DAO deposit input, causing the zip to pair the DAO deposit cell with a non-DAO output and the DAO withdrawal output with a non-DAO input — neither pair satisfies the "both must be DAO" guard, so the size check is silently skipped. The attacker then creates the withdrawal-preparation cell with a smaller lock script, which reduces `occupied_capacity` in `calculate_maximum_withdraw`, inflates `counted_capacity`, and yields more interest than the depositor is entitled to.

---

### Finding Description

**Root cause — positional zip in `DaoScriptSizeVerifier::verify()`** [1](#0-0) 

```rust
for (i, (input_meta, cell_output)) in self
    .resolved_transaction
    .resolved_inputs
    .iter()
    .zip(self.resolved_transaction.transaction.outputs())
    .enumerate()
```

The verifier pairs `resolved_inputs[i]` with `outputs[i]`. The guard that activates the size check requires **both** the input and the output at the same index to carry the DAO type script: [2](#0-1) 

If the attacker constructs the withdrawal-preparation transaction as:

| Index | Inputs | Outputs |
|-------|--------|---------|
| 0 | non-DAO cell (fee) | DAO withdrawal cell (small lock) |
| 1 | DAO deposit cell (large lock, data = `0x0000000000000000`) | change cell |

then the zip produces two pairs:
- `(non-DAO input[0], DAO output[0])` → guard fails (input is not DAO) → `continue`
- `(DAO deposit input[1], non-DAO output[1])` → guard fails (output is not DAO) → `continue`

The lock-script size check at line 885 is never reached: [3](#0-2) 

The code comment explicitly acknowledges this verifier is the **only** enforcement layer: [4](#0-3) 

> "It provides a temporary solution till Nervos DAO script can be properly upgraded."

**How the bypass inflates interest**

`calculate_maximum_withdraw` computes interest using the withdrawal cell's `occupied_capacity`: [5](#0-4) 

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

`counted_capacity = output_capacity − occupied_capacity`. Interest accrues on `counted_capacity`. If the attacker replaces a large lock script (e.g., 200-byte multisig, occupying ~2 CKB) with a minimal 53-byte secp256k1 lock (occupying ~0.53 CKB), the freed Δ ≈ 1.47 CKB is added to `counted_capacity` and earns interest at the full DAO rate — interest the attacker was never entitled to.

**Where the verifier is invoked (both tx-pool and block verification)** [6](#0-5) [7](#0-6) 

The same bypass applies in the block-verification path (`verification/contextual/src/contextual_block_verifier.rs` also imports and calls `DaoScriptSizeVerifier`).

---

### Impact Explanation

An attacker who has deposited CKB into NervosDAO with a large lock script can extract more CKB than they deposited plus legitimate interest. The surplus comes directly from the DAO secondary-issuance pool, which is shared by all depositors. The extra gain per withdrawal is:

```
extra_interest ≈ Δ_occupied_capacity × (withdrawing_ar / deposit_ar − 1)
```

For a 1.47 CKB reduction in occupied capacity over a 6-month deposit at ~3% annual DAO yield, the extra gain is small per transaction but scales with deposit size and lock-script size difference. An attacker with a very large lock script (e.g., a 10 000-byte script occupying ~100 CKB) switching to a minimal lock could extract ~3 CKB of unearned interest per 100 CKB deposited per year — a ~3% overcharge on the DAO pool per attacker position.

---

### Likelihood Explanation

Any CKB holder who can submit a transaction to the network can exploit this. No special privilege, key leak, or majority hashpower is required. The attacker only needs to:
1. Deposit CKB using a lock script larger than the minimum (e.g., any multisig or custom lock).
2. Submit a withdrawal-preparation transaction with a non-DAO input at index 0.

This is a straightforward, low-skill transaction construction reachable via the standard `send_transaction` RPC.

---

### Recommendation

Replace the positional `zip` with an explicit search: for each DAO deposit input (data = all zeros), find the corresponding DAO output by matching the cell's `out_point` or by requiring the protocol to mandate that the deposit input index equals the withdrawal output index and enforcing that constraint explicitly. Alternatively, iterate all inputs independently and verify that no DAO deposit input exists in the transaction unless a matching-index DAO output with the same lock-script size is also present.

---

### Proof of Concept

Construct a withdrawal-preparation transaction:

```
inputs:
  [0] non-DAO cell (e.g., a plain secp256k1 cell used to pay fees)
  [1] DAO deposit cell  (lock = large_multisig_script, data = 0x0000000000000000)

outputs:
  [0] DAO withdrawal cell  (lock = small_secp256k1_script, data = <deposit_block_number>)
  [1] change cell
```

Submit via `send_transaction` RPC. `DaoScriptSizeVerifier` iterates:
- pair `(inputs[0]=non-DAO, outputs[0]=DAO)` → `cell_uses_dao_type_script(inputs[0])` is `false` → `continue`
- pair `(inputs[1]=DAO deposit, outputs[1]=change)` → `cell_uses_dao_type_script(outputs[1])` is `false` → `continue`

No `DaoLockSizeMismatch` error is raised. The withdrawal cell with the smaller lock script is accepted on-chain. In the subsequent final-withdrawal transaction, `calculate_maximum_withdraw` computes interest on the inflated `counted_capacity`, paying out more CKB than the depositor is entitled to. [8](#0-7) [9](#0-8)

### Citations

**File:** verification/src/transaction_verifier.rs (L817-818)
```rust
/// Verifies that deposit cell and withdrawing cell in Nervos DAO use same sized lock scripts.
/// It provides a temporary solution till Nervos DAO script can be properly upgraded.
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

**File:** tx-pool/src/util.rs (L111-114)
```rust
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
```

**File:** tx-pool/src/util.rs (L121-128)
```rust
                    DaoScriptSizeVerifier::new(
                        rtx,
                        snapshot.cloned_consensus(),
                        snapshot.as_data_loader(),
                    )
                    .verify()?;
                    Ok(result)
                })
```
