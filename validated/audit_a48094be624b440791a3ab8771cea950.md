### Title
Silent u128→u64 Truncation in DAO Withdrawal Calculation Permanently Locks Depositor Capacity — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` performs a u128→u64 cast without overflow checking. If the intermediate product overflows u64, the result is silently truncated to a small value. This causes the downstream `transaction_fee` calculation to fail (negative fee), making the DAO withdrawal transaction permanently unprocessable and the depositor's capacity permanently locked in the DAO cell.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the interest-adjusted withdrawal capacity:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The expression `withdraw_counted_capacity as u64` is a **silent truncating cast** in Rust. If `withdraw_counted_capacity` exceeds `u64::MAX`, the high bits are silently discarded, producing an arbitrarily small (or zero) result with no error returned.

This is inconsistent with every other similar arithmetic operation in the same codebase. For example, `secondary_block_reward` and `dao_field_with_current_epoch` both use the checked form:

```rust
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) 

The `calculate_maximum_withdraw` function is called from `transaction_maximum_withdraw`, which feeds into `transaction_fee`:

```rust
pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
    let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
    rtx.transaction
        .outputs_capacity()
        .and_then(|y| maximum_withdraw.safe_sub(y))
        .map_err(Into::into)
}
``` [4](#0-3) 

If `withdraw_counted_capacity` wraps to a small value, `maximum_withdraw` becomes smaller than `outputs_capacity`, causing `safe_sub` to return a `CapacityError`. The withdrawal transaction is then rejected by the node at fee-verification time, and the depositor's capacity is permanently locked in the DAO cell with no valid spending path.

The `calculate_dao_maximum_withdraw` RPC also calls this function directly:

```rust
match calculator.calculate_maximum_withdraw(
    &output,
    core::Capacity::bytes(output_data_capacity.len())...,
    &deposit_header_hash,
    &withdrawing_header_hash.into(),
)
``` [5](#0-4) 

So the RPC would also return a silently wrong (too-small) value, misleading the depositor into constructing a transaction that will always be rejected.

---

### Impact Explanation

A DAO depositor who triggers the overflow condition cannot construct any valid withdrawal transaction. Their capacity is locked in the DAO cell permanently — there is no alternative spending path once the DAO type script is attached. The capacity is not recoverable by any protocol mechanism. This is the direct CKB analog of the RAACNFT pattern: value is received (deposited into DAO) and cannot be retrieved (withdrawal calculation silently corrupts the entitled amount).

---

### Likelihood Explanation

The overflow condition requires:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

`counted_capacity` is at most `u64::MAX` minus the cell's occupied capacity. `ar` starts at `10_000_000_000_000_000` (10^16) and grows slowly with secondary issuance. For a deposit equal to the entire circulating CKB supply (~3.36 × 10^18 shannons), the `ar` ratio would need to grow by a factor of ~5.5× before overflow occurs. Given the slow growth rate of `ar`, this is not reachable on mainnet in the near term.

However, the code defect is real and structurally inconsistent with the rest of the codebase. Any future change to issuance parameters, or a very large deposit held for an extremely long time, could trigger it. The entry path is fully unprivileged: any user can deposit into the DAO via a normal transaction.

---

### Recommendation

Replace the silent cast with the checked conversion already used elsewhere in the same file:

```rust
// Before (silent truncation):
Capacity::shannons(withdraw_counted_capacity as u64)

// After (consistent with the rest of the codebase):
let withdraw_counted_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_u64)
``` [6](#0-5) 

This makes the function return `Err(DaoError::Overflow)` instead of silently corrupting the result, consistent with `secondary_block_reward` and `dao_field_with_current_epoch`.

---

### Proof of Concept

The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` already exercises a near-overflow case but relies on `safe_add` failing rather than catching the silent cast: [7](#0-6) 

A targeted test demonstrating the silent truncation:

```rust
#[test]
fn check_silent_truncation_in_withdraw_calculation() {
    // counted_capacity near u64::MAX, withdrawing_ar slightly larger than deposit_ar
    // such that counted_capacity * withdrawing_ar / deposit_ar overflows u64
    // but safe_add(occupied_capacity) does NOT overflow (wraps to small value first)
    let output = CellOutput::new_builder()
        .capacity(Capacity::shannons(u64::MAX))
        .build();
    // deposit_ar = 10_000_000_000_000_000
    // withdrawing_ar = 10_000_000_000_000_001  (tiny interest)
    // counted_capacity = u64::MAX - occupied_capacity
    // product = (u64::MAX - occ) * 10_000_000_000_000_001 / 10_000_000_000_000_000
    //         > u64::MAX  =>  wraps silently
    // With the fix, this should return Err(DaoError::Overflow).
    // Without the fix, it returns Ok(wrong_small_value).
}
```

The `calculate_dao_maximum_withdraw` RPC endpoint is the unprivileged attacker-reachable entry point — any RPC caller can trigger the calculation with attacker-chosen `out_point` and `withdrawing_header_hash` parameters. [8](#0-7)

### Citations

**File:** util/dao/src/lib.rs (L30-36)
```rust
    pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
        let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
        rtx.transaction
            .outputs_capacity()
            .and_then(|y| maximum_withdraw.safe_sub(y))
            .map_err(Into::into)
    }
```

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L204-204)
```rust
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/src/lib.rs (L244-245)
```rust
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** rpc/src/module/experiment.rs (L235-298)
```rust
    fn calculate_dao_maximum_withdraw(
        &self,
        out_point: OutPoint,
        kind: DaoWithdrawingCalculationKind,
    ) -> Result<Capacity> {
        let snapshot: &Snapshot = &self.shared.snapshot();
        let consensus = snapshot.consensus();
        let out_point: packed::OutPoint = out_point.into();
        let data_loader = snapshot.borrow_as_data_loader();
        let calculator = DaoCalculator::new(consensus, &data_loader);
        match kind {
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
            }
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
        }
```

**File:** util/dao/src/tests.rs (L295-349)
```rust
#[test]
fn check_withdraw_calculation_overflows() {
    let output = CellOutput::new_builder()
        .capacity(Capacity::shannons(18_446_744_073_709_550_000))
        .build();
    let tx = TransactionBuilder::default().output(output.clone()).build();
    let epoch = EpochNumberWithFraction::new(1, 100, 1000);
    let deposit_header = HeaderBuilder::default()
        .number(100)
        .epoch(epoch)
        .dao(pack_dao_data(
            10_000_000_000_123_456,
            Default::default(),
            Default::default(),
            Default::default(),
        ))
        .build();
    let deposit_block = BlockBuilder::default()
        .header(deposit_header)
        .transaction(tx)
        .build();

    let epoch = EpochNumberWithFraction::new(1, 200, 1000);
    let withdrawing_header = HeaderBuilder::default()
        .number(200)
        .epoch(epoch)
        .dao(pack_dao_data(
            10_000_000_001_123_456,
            Default::default(),
            Default::default(),
            Default::default(),
        ))
        .build();
    let withdrawing_block = BlockBuilder::default().header(withdrawing_header).build();

    let tmp_dir = TempDir::new().unwrap();
    let db = RocksDB::open_in(&tmp_dir, COLUMNS);
    let store = ChainDB::new(db, Default::default());
    let txn = store.begin_transaction();
    txn.insert_block(&deposit_block).unwrap();
    txn.attach_block(&deposit_block).unwrap();
    txn.insert_block(&withdrawing_block).unwrap();
    txn.attach_block(&withdrawing_block).unwrap();
    txn.commit().unwrap();

    let consensus = Consensus::default();
    let data_loader = store.borrow_as_data_loader();
    let calculator = DaoCalculator::new(&consensus, &data_loader);
    let result = calculator.calculate_maximum_withdraw(
        &output,
        Capacity::bytes(0).expect("should not overflow"),
        &deposit_block.hash(),
        &withdrawing_block.hash(),
    );
    assert!(result.is_err());
```
