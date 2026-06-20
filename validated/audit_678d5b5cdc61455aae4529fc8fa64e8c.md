### Title
`calculate_dao_maximum_withdraw` (Option 1) Returns Inflated Capacity When Phase-1 Lock Script Differs From Deposit Cell — (`rpc/src/module/experiment.rs`)

---

### Summary

The `calculate_dao_maximum_withdraw` RPC with `WithdrawingHeaderHash` (option 1) computes `occupied_capacity` from the **deposit cell's** output structure. However, the actual phase-2 withdrawal transaction's capacity enforcement (`DaoCalculator::transaction_maximum_withdraw`) computes `occupied_capacity` from the **phase-1 (prepare) cell's** output structure. For DAO cells deposited before `starting_block_limiting_dao_withdrawing_lock`, the `DaoScriptSizeVerifier` does not enforce lock script size equality between deposit and phase-1 cells. When the phase-1 lock script is larger, the RPC overestimates the maximum withdrawal capacity. Any wallet or DApp that uses the returned value as the output capacity for the phase-2 withdrawal transaction will construct a transaction that fails on-chain.

---

### Finding Description

**Option 1 of `calculate_dao_maximum_withdraw` (`WithdrawingHeaderHash`)** reads the deposit transaction's output and data, then calls `DaoCalculator::calculate_maximum_withdraw` with those values:

```rust
// rpc/src/module/experiment.rs lines 246-267
DaoWithdrawingCalculationKind::WithdrawingHeaderHash(withdrawing_header_hash) => {
    let (tx, deposit_header_hash) = snapshot.get_transaction(&out_point.tx_hash())...;
    let output = tx.outputs().get(out_point.index().into())...;       // deposit cell output
    let output_data = tx.outputs_data().get(out_point.index().into())...;  // deposit cell data
    calculator.calculate_maximum_withdraw(
        &output,
        core::Capacity::bytes(output_data.len()),  // deposit cell data length
        &deposit_header_hash,
        &withdrawing_header_hash.into(),
    )
}
```

The `calculate_maximum_withdraw` function computes `occupied_capacity` from the passed `output` (the deposit cell's lock script and type script) and `output_data_capacity`:

```rust
// util/dao/src/lib.rs lines 149-156
let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
let output_capacity: Capacity = output.capacity().into();
let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

However, during actual phase-2 withdrawal verification, `DaoCalculator::transaction_maximum_withdraw` uses `cell_meta.cell_output` and `cell_meta.data_bytes` — which are the **phase-1 cell's** properties, not the deposit cell's:

```rust
// util/dao/src/lib.rs lines 108-113
self.calculate_maximum_withdraw(
    output,                                              // phase-1 cell output
    Capacity::bytes(cell_meta.data_bytes as usize)?,    // phase-1 cell data bytes
    deposit_header_hash,
    withdrawing_header_hash,
)
```

The `DaoScriptSizeVerifier` enforces that the phase-1 lock script size must equal the deposit lock script size — but **only for cells committed at or after `starting_block_limiting_dao_withdrawing_lock`**:

```rust
// verification/src/transaction_verifier.rs lines 874-881
if let Some(info) = &input_meta.transaction_info
    && info.block_number
        < self.consensus.starting_block_limiting_dao_withdrawing_lock()
{
    continue;  // skip size check for old cells
}
```

For deposit cells committed **before** `starting_block_limiting_dao_withdrawing_lock`, the phase-1 prepare transaction is permitted to use a lock script of a different size. When the phase-1 lock script is larger than the deposit lock script:

- `occupied_capacity_deposit` < `occupied_capacity_phase1`
- `counted_capacity_deposit` > `counted_capacity_phase1` (assuming same capacity field value)
- RPC returns: `counted_capacity_deposit × ar_ratio + occupied_capacity_deposit` (inflated)
- Actual max: `counted_capacity_phase1 × ar_ratio + occupied_capacity_phase1` (smaller)

The RPC documentation acknowledges this assumption: *"the calculation of occupied capacity will be based on the depositing transaction's output, assuming the output of phase 1 transaction is the same as the depositing transaction's output."* However, this assumption is not enforced for old cells, and the RPC does not warn callers when the assumption is violated.

---

### Impact Explanation

A wallet or DApp that follows the natural usage pattern:

```
capacity = calculate_dao_maximum_withdraw(deposit_out_point, withdrawing_header_hash)
build_phase2_tx(output_capacity = capacity)
send_transaction(phase2_tx)  // FAILS
```

will construct a phase-2 withdrawal transaction where `outputs_capacity > transaction_maximum_withdraw`. The `DaoCalculator::transaction_fee` call (which does `maximum_withdraw.safe_sub(outputs_capacity)`) will return an error, and the transaction will be rejected by the tx-pool and by block verification. The user cannot withdraw their DAO funds using the RPC-provided value. This is a direct analog to the GoGoPool `maxWithdraw()` bug: a "maximum" view function returns a value that, when used in the actual operation, causes a revert/rejection.

---

### Likelihood Explanation

The affected population is DAO depositors who:
1. Deposited CKB before `starting_block_limiting_dao_withdrawing_lock` was activated (a mainnet hard fork), **and**
2. Created a phase-1 prepare transaction with a larger lock script (e.g., migrating to a lock with longer args).

This is a realistic scenario for early mainnet participants who changed their lock script during the prepare phase. Any wallet that calls `calculate_dao_maximum_withdraw` with option 1 and uses the result directly as the output capacity is affected. The RPC is publicly accessible to any unprivileged RPC caller.

---

### Recommendation

In the `WithdrawingHeaderHash` branch of `calculate_dao_maximum_withdraw`, the RPC should either:

1. **Warn callers** in the return value or documentation that the result may be inflated if the phase-1 lock script differs from the deposit lock script, and advise using option 2 (`WithdrawingOutPoint`) once the phase-1 transaction is confirmed; or
2. **Clamp the result** to the actual maximum by also computing `occupied_capacity` using the phase-1 cell's structure when the phase-1 transaction is already on-chain; or
3. **Document clearly** that option 1 is only safe when the phase-1 lock script size equals the deposit lock script size, and that option 2 (`WithdrawingOutPoint`) should always be preferred for confirmed phase-1 transactions.

---

### Proof of Concept

**Setup:**
- Deposit cell committed at block 1000 (before `starting_block_limiting_dao_withdrawing_lock`)
- Deposit cell: capacity = 1,000,000 CKB, lock script args = 20 bytes → `occupied_capacity_deposit` = 8 + (20+32+1) + (8+32+1) = 102 bytes = 10,200 shannons
- Phase-1 cell: same capacity = 1,000,000 CKB, lock script args = 40 bytes (user migrated lock) → `occupied_capacity_phase1` = 8 + (40+32+1) + (8+32+1) = 122 bytes = 12,200 shannons
- `deposit_ar` = 10,000,000,000,000,000; `withdrawing_ar` = 10,000,000,001,000,000 (0.01% interest)

**RPC option 1 result:**
- `counted_capacity_deposit` = 1,000,000 CKB - 10,200 shannons = 99,999,989,800 shannons
- `withdraw_counted_capacity` = 99,999,989,800 × (10,000,000,001,000,000 / 10,000,000,000,000,000) ≈ 99,999,999,798 shannons
- RPC returns ≈ 99,999,999,798 + 10,200 = **99,999,999,998 + 10,200 shannons**

**Actual phase-2 maximum:**
- `counted_capacity_phase1` = 1,000,000 CKB - 12,200 shannons = 99,999,987,800 shannons
- `withdraw_counted_capacity` = 99,999,987,800 × ratio ≈ 99,999,997,798 shannons
- Actual max ≈ 99,999,997,798 + 12,200 = **99,999,997,798 + 12,200 shannons**

The RPC overestimates by approximately `(10,200 - 12,200) × (ar_ratio - 1) + (12,200 - 10,200)` shannons. A wallet using the RPC result as output capacity would construct a transaction where `outputs_capacity > actual_maximum_withdraw`, causing the tx-pool to reject it with a fee calculation error. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rpc/src/module/experiment.rs (L246-267)
```rust
            DaoWithdrawingCalculationKind::WithdrawingHeaderHash(withdrawing_header_hash) => {
                let (tx, deposit_header_hash) = snapshot
                    .get_transaction(&out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;
                let output = tx
                    .outputs()
                    .get(out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;
                let output_data = tx
                    .outputs_data()
                    .get(out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;

                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash.into(),
                ) {
                    Ok(capacity) => Ok(capacity.into()),
                    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
                }
```

**File:** util/dao/src/lib.rs (L108-113)
```rust
                            self.calculate_maximum_withdraw(
                                output,
                                Capacity::bytes(cell_meta.data_bytes as usize)?,
                                deposit_header_hash,
                                withdrawing_header_hash,
                            )
```

**File:** util/dao/src/lib.rs (L149-158)
```rust
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

**File:** verification/src/transaction_verifier.rs (L874-881)
```rust
            if let Some(info) = &input_meta.transaction_info
                && info.block_number
                    < self
                        .consensus
                        .starting_block_limiting_dao_withdrawing_lock()
            {
                continue;
            }
```

**File:** rpc/README.md (L2140-2143)
```markdown
option 1, the assumed reference block hash for withdrawing phase 1 transaction, this block must be in the
[canonical chain](trait.ChainRpc.html#canonical-chain), the calculation of occupied capacity will be based on the depositing transaction's output, assuming the output of phase 1 transaction is the same as the depositing transaction's output.

option 2, the out point of the withdrawing phase 1 transaction, the calculation of occupied capacity will be based on corresponding phase 1 transaction's output.
```
