### Title
Wrong Transaction Output Used in DAO Maximum Withdrawal Calculation — (`rpc/src/module/experiment.rs`)

### Summary

The `calculate_dao_maximum_withdraw` RPC function contains two branches for computing the maximum DAO withdrawal. In the `WithdrawingOutPoint` branch, the deposit transaction is fetched and immediately discarded (`_tx`), while the **withdrawal transaction's** output is passed to `calculator.calculate_maximum_withdraw(...)` instead of the **deposit transaction's** output. This is a direct analog to the external report's pattern: a parameter/variable is obtained correctly but the wrong variable is forwarded to the critical internal call.

### Finding Description

In `rpc/src/module/experiment.rs`, the `WithdrawingHeaderHash` branch (Branch 1) correctly fetches the deposit transaction and uses its output: [1](#0-0) 

```rust
let (tx, deposit_header_hash) = snapshot
    .get_transaction(&out_point.tx_hash())...;
let output = tx.outputs().get(out_point.index().into())...;
let output_data = tx.outputs_data().get(out_point.index().into())...;
match calculator.calculate_maximum_withdraw(
    &output,
    core::Capacity::bytes(output_data.len())...,
    &deposit_header_hash,
    &withdrawing_header_hash.into(),
)
```

The `WithdrawingOutPoint` branch (Branch 2) fetches the deposit transaction as `_tx` but then **discards it entirely**, instead fetching the withdrawal transaction and using its output: [2](#0-1) 

```rust
let (_tx, deposit_header_hash) = snapshot          // deposit tx fetched but IGNORED
    .get_transaction(&out_point.tx_hash())...;

let (withdrawing_tx, withdrawing_header_hash) = snapshot
    .get_transaction(&withdrawing_out_point.tx_hash())...;

let output = withdrawing_tx                         // WRONG: should be _tx
    .outputs()
    .get(withdrawing_out_point.index().into())...;
let output_data = withdrawing_tx                    // WRONG: should be _tx
    .outputs_data()
    .get(withdrawing_out_point.index().into())...;

match calculator.calculate_maximum_withdraw(
    &output,                                        // withdrawal cell output, not deposit cell output
    core::Capacity::bytes(output_data.len())...,
    &deposit_header_hash,
    &withdrawing_header_hash,
)
```

The `calculate_maximum_withdraw` function expects the **deposit cell's** `CellOutput` and data capacity to compute the interest-bearing withdrawal amount: [3](#0-2) 

### Impact Explanation

`calculate_maximum_withdraw` uses `output.capacity()` as the deposited principal to compute the DAO interest formula (`deposit_capacity × AR_withdrawing / AR_deposit`). When the withdrawal cell's output is passed instead of the deposit cell's output:

- If the withdrawal cell has a **different capacity** than the deposit cell (e.g., the user adjusted capacity during the withdrawal phase), the returned maximum withdrawal amount is incorrect.
- If the withdrawal cell's data length differs from the deposit cell's data length, `output_data_capacity` is also wrong, further skewing the result.
- A user relying on this RPC to construct their final withdrawal transaction may claim an incorrect amount: too high (transaction rejected by consensus, funds temporarily locked) or too low (user permanently loses accrued DAO interest). [4](#0-3) 

### Likelihood Explanation

Any unprivileged RPC caller can invoke `calculate_dao_maximum_withdraw` with `DaoWithdrawingCalculationKind::WithdrawingOutPoint`. The bug is unconditionally triggered on every call to this branch. Wallets and tooling that use this RPC to compute expected DAO returns and then construct withdrawal transactions are directly affected. The deposit transaction is always fetched (proving the intent was to use it) but is silently discarded. [5](#0-4) 

### Recommendation

In the `WithdrawingOutPoint` branch, fetch `output` and `output_data` from the **deposit transaction** (`_tx`) at `out_point.index()`, not from `withdrawing_tx` at `withdrawing_out_point.index()`. Rename `_tx` to `tx` and use it consistently, mirroring the correct pattern in Branch 1:

```rust
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
```

### Proof of Concept

1. Create a DAO deposit cell with capacity `C_deposit` and 8 bytes of zero data.
2. Create a withdrawal transaction spending the deposit cell, producing a withdrawal cell with capacity `C_withdraw ≠ C_deposit` (e.g., slightly reduced to pay fees from the cell itself).
3. Call `calculate_dao_maximum_withdraw` with `WithdrawingOutPoint` pointing to the withdrawal cell.
4. The RPC computes interest on `C_withdraw` instead of `C_deposit`, returning a value that does not match the actual on-chain DAO formula.
5. A wallet constructing the final withdrawal transaction using this value will produce a transaction that either over-claims (rejected) or under-claims (interest lost). [6](#0-5)

### Citations

**File:** rpc/src/module/experiment.rs (L247-264)
```rust
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
```

**File:** rpc/src/module/experiment.rs (L269-297)
```rust
            DaoWithdrawingCalculationKind::WithdrawingOutPoint(withdrawing_out_point) => {
                let (_tx, deposit_header_hash) = snapshot
                    .get_transaction(&out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;

                let withdrawing_out_point: packed::OutPoint = withdrawing_out_point.into();
                let (withdrawing_tx, withdrawing_header_hash) = snapshot
                    .get_transaction(&withdrawing_out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid withdrawing_out_point"))?;

                let output = withdrawing_tx
                    .outputs()
                    .get(withdrawing_out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid withdrawing_out_point"))?;
                let output_data = withdrawing_tx
                    .outputs_data()
                    .get(withdrawing_out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid withdrawing_out_point"))?;

                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash,
                ) {
                    Ok(capacity) => Ok(capacity.into()),
                    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
                }
            }
```

**File:** util/dao/src/lib.rs (L28-36)
```rust
impl<'a, DL: CellDataProvider + HeaderProvider> DaoCalculator<'a, DL> {
    /// Returns the total transactions fee of `rtx`.
    pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
        let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
        rtx.transaction
            .outputs_capacity()
            .and_then(|y| maximum_withdraw.safe_sub(y))
            .map_err(Into::into)
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
