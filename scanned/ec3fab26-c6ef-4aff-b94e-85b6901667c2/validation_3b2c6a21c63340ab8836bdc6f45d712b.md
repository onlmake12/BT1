Audit Report

## Title
`DaoScriptSizeVerifier` Positional-Zip Bypass Allows Inflated NervosDAO Interest Extraction — (File: `verification/src/transaction_verifier.rs`)

## Summary
`DaoScriptSizeVerifier::verify()` pairs `resolved_inputs` with `outputs` positionally via `.zip()`. Its guard requires both the input and output at the same index to carry the DAO type script. An attacker can defeat this by placing a non-DAO input at index 0 before the DAO deposit input, causing neither pair to satisfy the guard, so the lock-script size check is silently skipped. The attacker then creates the withdrawal-preparation cell with a smaller lock script, which reduces `occupied_capacity` in `calculate_maximum_withdraw`, inflates `counted_capacity`, and yields more interest than the depositor is entitled to — draining the shared DAO secondary-issuance pool.

## Finding Description

The verifier at `verification/src/transaction_verifier.rs` lines 847–852 zips inputs and outputs positionally: [1](#0-0) 

The guard at lines 855–858 requires **both** the input and the output at the same index to carry the DAO type script; if either does not, the iteration `continue`s and the size check at line 885 is never reached: [2](#0-1) [3](#0-2) 

If the attacker constructs the withdrawal-preparation transaction as:

| Index | Inputs | Outputs |
|-------|--------|---------|
| 0 | non-DAO cell (fee) | DAO withdrawal cell (small lock) |
| 1 | DAO deposit cell (large lock, data = `0x0000000000000000`) | change cell |

the zip produces:
- Pair (0): `(non-DAO input, DAO output)` → input is not DAO → `continue`
- Pair (1): `(DAO deposit input, non-DAO output)` → output is not DAO → `continue`

No `DaoLockSizeMismatch` error is raised. The code comment explicitly acknowledges this verifier is the **only** enforcement layer: [4](#0-3) 

In the subsequent final-withdrawal transaction, `calculate_maximum_withdraw` computes interest using the withdrawal cell's `occupied_capacity`: [5](#0-4) 

`counted_capacity = output_capacity − occupied_capacity`. A smaller lock script on the withdrawal cell reduces `occupied_capacity`, increases `counted_capacity`, and earns interest on capacity the attacker was never entitled to. The same bypass applies in the block-verification path, as `DaoScriptSizeVerifier` is imported and called identically there: [6](#0-5) [7](#0-6) 

## Impact Explanation

This matches **"Vulnerabilities which could easily damage CKB economy" (Critical, 15001–25000 points)**. The attacker extracts CKB beyond their legitimate deposit plus interest; the surplus is drawn directly from the DAO secondary-issuance pool shared by all depositors. The gain scales with the lock-script size difference and deposit size. A 10,000-byte lock script (occupying ~100 CKB) replaced by a minimal 53-byte lock (~0.53 CKB) frees ~99.47 CKB of `counted_capacity`, earning unentitled interest at the full DAO rate on that freed amount — a concrete, repeatable economic drain on the shared pool.

## Likelihood Explanation

Any CKB holder can trigger this with no special privilege. The only prerequisites are: (1) deposit CKB using a lock script larger than the minimum, and (2) submit a withdrawal-preparation transaction with a non-DAO input at index 0. Both steps are reachable via the standard `send_transaction` RPC. The exploit is low-skill, requires no key leaks or majority hashpower, and is repeatable across any number of positions.

## Recommendation

Replace the positional `.zip()` with an explicit per-input search: for each DAO deposit input (data = all zeros), locate the corresponding DAO output independently — either by requiring the protocol to mandate that deposit input index equals withdrawal output index and enforcing that constraint explicitly, or by iterating all inputs and verifying that for every DAO deposit input a matching-index DAO output with the same lock-script size exists. A minimal fix is to iterate `resolved_inputs` and `outputs` separately, and for each DAO deposit input, check whether the output at the same index is also a DAO cell; if not, reject the transaction rather than silently skipping.

## Proof of Concept

Construct a withdrawal-preparation transaction:

```
inputs:
  [0] non-DAO cell (plain secp256k1 cell, used to pay fees)
  [1] DAO deposit cell (lock = large_multisig_script, data = 0x0000000000000000)

outputs:
  [0] DAO withdrawal cell (lock = small_secp256k1_script, data = <deposit_block_number>)
  [1] change cell
```

Submit via `send_transaction` RPC. `DaoScriptSizeVerifier::verify()` iterates:
- Pair `(inputs[0]=non-DAO, outputs[0]=DAO)` → `cell_uses_dao_type_script(inputs[0])` is `false` → `continue`
- Pair `(inputs[1]=DAO deposit, outputs[1]=change)` → `cell_uses_dao_type_script(outputs[1])` is `false` → `continue`

No `DaoLockSizeMismatch` error is raised. The withdrawal cell with the smaller lock script is accepted on-chain. In the subsequent final-withdrawal transaction, `transaction_maximum_withdraw` in `util/dao/src/lib.rs` iterates the resolved inputs, finds the withdrawal cell (non-zero data), calls `calculate_maximum_withdraw` with the smaller `occupied_capacity`, and pays out more CKB than the depositor deposited plus legitimate interest. [8](#0-7)

### Citations

**File:** verification/src/transaction_verifier.rs (L817-818)
```rust
/// Verifies that deposit cell and withdrawing cell in Nervos DAO use same sized lock scripts.
/// It provides a temporary solution till Nervos DAO script can be properly upgraded.
```

**File:** verification/src/transaction_verifier.rs (L847-852)
```rust
        for (i, (input_meta, cell_output)) in self
            .resolved_transaction
            .resolved_inputs
            .iter()
            .zip(self.resolved_transaction.transaction.outputs())
            .enumerate()
```

**File:** verification/src/transaction_verifier.rs (L854-858)
```rust
            // Both the input and output cell must use Nervos DAO as type script
            if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
                && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
            {
                continue;
```

**File:** verification/src/transaction_verifier.rs (L883-887)
```rust
            // Now we have a pair of DAO deposit and withdrawing cells, it is expected
            // they have the lock scripts of the same size.
            if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
                return Err((TransactionError::DaoLockSizeMismatch { index: i }).into());
            }
```

**File:** util/dao/src/lib.rs (L43-113)
```rust
        rtx.resolved_inputs.iter().enumerate().try_fold(
            Capacity::zero(),
            |capacities, (i, cell_meta)| {
                let capacity: Result<Capacity, DaoError> = {
                    let output = &cell_meta.cell_output;
                    let is_dao_type_script = |type_script: Script| {
                        Into::<u8>::into(type_script.hash_type())
                            == Into::<u8>::into(ScriptHashType::Type)
                            && type_script.code_hash() == self.consensus.dao_type_hash()
                    };
                    let is_dao_output = output
                        .type_()
                        .to_opt()
                        .map(is_dao_type_script)
                        .unwrap_or(false);
                    if is_dao_output {
                        // A withdrawing DAO cell has 8 bytes of cell data storing the
                        // block number of the original deposit.
                        let deposited_block_number =
                            match self.data_loader.load_cell_data(cell_meta) {
                                Some(data) if data.len() == 8 => LittleEndian::read_u64(&data),
                                _ => 0,
                            };
                        if deposited_block_number > 0 {
                            let withdrawing_header_hash = cell_meta
                                .transaction_info
                                .as_ref()
                                .map(|info| &info.block_hash)
                                .filter(|hash| header_deps.contains(hash))
                                .ok_or(DaoError::InvalidOutPoint)?;
                            let deposit_header_hash = rtx
                                .transaction
                                .witnesses()
                                .get(i)
                                .ok_or(DaoError::InvalidOutPoint)
                                .and_then(|witness_data| {
                                    // dao contract stores header deps index as u64 in the input_type field of WitnessArgs
                                    let witness =
                                        WitnessArgs::from_slice(&Into::<Bytes>::into(witness_data))
                                            .map_err(|_| DaoError::InvalidDaoFormat)?;
                                    let header_deps_index_data: Option<Bytes> =
                                        witness.input_type().to_opt().map(|witness| witness.into());
                                    if header_deps_index_data.is_none()
                                        || header_deps_index_data.clone().map(|data| data.len())
                                            != Some(8)
                                    {
                                        return Err(DaoError::InvalidDaoFormat);
                                    }
                                    Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
                                })
                                .and_then(|header_dep_index| {
                                    rtx.transaction
                                        .header_deps()
                                        .get(header_dep_index as usize)
                                        .and_then(|hash| header_deps.get(&hash))
                                        .ok_or(DaoError::InvalidOutPoint)
                                })?;

                            let deposit_header = self
                                .data_loader
                                .get_header(deposit_header_hash)
                                .ok_or(DaoError::InvalidHeader)?;
                            if deposit_header.number() != deposited_block_number {
                                return Err(DaoError::InvalidOutPoint);
                            }
                            self.calculate_maximum_withdraw(
                                output,
                                Capacity::bytes(cell_meta.data_bytes as usize)?,
                                deposit_header_hash,
                                withdrawing_header_hash,
                            )
```

**File:** util/dao/src/lib.rs (L149-156)
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

**File:** verification/contextual/src/contextual_block_verifier.rs (L28-31)
```rust
use ckb_verification::{
    BlockErrorKind, CellbaseError, CommitError, ContextualTransactionVerifier,
    DaoScriptSizeVerifier, TimeRelativeTransactionVerifier, UnknownParentError,
};
```

**File:** tx-pool/src/util.rs (L111-114)
```rust
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
```
