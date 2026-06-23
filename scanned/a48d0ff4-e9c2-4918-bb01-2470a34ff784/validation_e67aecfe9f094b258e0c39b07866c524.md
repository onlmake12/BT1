### Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Produces Incorrect DAO Withdrawal Capacity — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes `withdraw_counted_capacity` as a `u128` intermediate value, then casts it to `u64` using the unchecked `as u64` operator. Every other analogous u128→u64 conversion in the same file uses the checked `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern. If `withdraw_counted_capacity` exceeds `u64::MAX`, the result silently truncates to `withdraw_counted_capacity mod 2^64` — a wrong, smaller value — causing the user to receive less than their correct DAO withdrawal amount and corrupting the DAO accounting field written into the chain.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the withdrawal capacity as:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

The `as u64` cast at line 156 is a **truncating, non-panicking, non-erroring** cast. If `withdraw_counted_capacity > u64::MAX`, Rust silently takes the lowest 64 bits, producing an arbitrarily wrong (smaller) value. No error is returned; the caller receives `Ok(wrong_capacity)`.

Contrast this with every other u128→u64 conversion in the same file:

- `secondary_block_reward` (line 204): `u64::try_from(reward128).map_err(|_| DaoError::Overflow)?`
- `dao_field_with_current_epoch` (line 245): `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?`
- `dao_field_with_current_epoch` (line 258): `u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?`

All three use checked conversion and propagate `DaoError::Overflow`. The `calculate_maximum_withdraw` function is the sole exception.

The overflow condition is:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

Since `counted_capacity ≤ u64::MAX` and `deposit_ar` starts at `10^10`, overflow requires `withdrawing_ar / deposit_ar` to be sufficiently large. The AR ratio grows with secondary issuance over time; a depositor who holds a near-maximum-capacity cell for a very long period, or who deposits when `deposit_ar` is small and withdraws when `withdrawing_ar` is large, can reach this condition. The existing test `check_withdraw_calculation_overflows` only exercises the downstream `safe_add` overflow path — it does not exercise the `as u64` truncation path, leaving the bug untested and undetected.

The function is called from two reachable paths:

1. **Block processing**: `withdrawed_interests` → `transaction_maximum_withdraw` → `calculate_maximum_withdraw`. The result feeds directly into `current_s` in `dao_field_with_current_epoch`, which is written into the block's DAO field.
2. **RPC**: `ExperimentRpcImpl::calculate_dao_maximum_withdraw` calls `DaoCalculator::calculate_maximum_withdraw` directly and returns the result to any RPC caller.

---

### Impact Explanation

When `withdraw_counted_capacity` overflows `u64`, the truncated value is `withdraw_counted_capacity mod 2^64`, which can be arbitrarily small (including zero). The consequences are:

1. **Loss of funds for the depositor**: The user's DAO withdrawal transaction is accepted with an output capacity far below the correct amount. The DAO type script enforces the amount computed by this function; if the function returns a wrong value, the type script enforces the wrong amount.
2. **Corrupted DAO accounting**: `withdrawed_interests` is computed as `maximum_withdraws - input_capacities`. If `maximum_withdraws` is silently truncated, `withdrawed_interests` is wrong, and `current_s` (the DAO surplus field) in the block header is wrong. This propagates to all future DAO interest calculations that depend on `current_s`.
3. **Incorrect RPC output**: `calculate_dao_maximum_withdraw` returns a wrong value to any caller, misleading wallets and users about their expected withdrawal amount.

---

### Likelihood Explanation

The overflow requires `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`. In practice, `deposit_ar` starts at `10_000_000_000_000_000` (10^16) and grows slowly via secondary issuance (~1.344 billion CKB/year). For a cell with `counted_capacity` near `u64::MAX` (~18.4 × 10^18 shannons), the AR ratio must exceed approximately 1.0 for overflow to occur — which it always does, since `withdrawing_ar ≥ deposit_ar`. However, the ratio must exceed `u64::MAX / counted_capacity`, which for near-maximum cells is just above 1.0. Given that `withdrawing_ar` grows monotonically and the ratio can accumulate over years, a long-lived large-capacity DAO deposit is a realistic trigger. The inconsistency with the rest of the codebase — where every analogous conversion is checked — strongly indicates this is an unintentional oversight rather than a deliberate design choice.

---

### Recommendation

Replace the unchecked cast on line 156 with the same checked pattern used everywhere else in the file:

```rust
// Before (line 155-156):
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After:
let withdraw_capacity = Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
)
.safe_add(occupied_capacity)?;
```

This makes `calculate_maximum_withdraw` consistent with `secondary_block_reward` and `dao_field_with_current_epoch`, and ensures that any overflow is surfaced as a `DaoError::Overflow` rather than silently producing a wrong result.

---

### Proof of Concept

The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` (lines 295–350) uses `capacity = 18_446_744_073_709_550_000` and AR values `10_000_000_001_123_456 / 10_000_000_000_123_456`. Tracing through:

- `counted_capacity ≈ 18_446_744_069_609_550_000`
- `withdraw_counted_capacity ≈ 18_446_744_071_454_224_406` — this is **below** `u64::MAX`, so `as u64` does not truncate
- `withdraw_capacity = 18_446_744_071_454_224_406 + 4_100_000_000 > u64::MAX` → `safe_add` returns `Err`

The test passes because `safe_add` catches the overflow, **not** because the `as u64` cast is safe. The `as u64` truncation path is never exercised.

To trigger the `as u64` truncation specifically, construct a scenario where `withdraw_counted_capacity > u64::MAX` but `(withdraw_counted_capacity mod 2^64) + occupied_capacity ≤ u64::MAX`:

```
counted_capacity = u64::MAX  (≈ 18.4 × 10^18 shannons)
withdrawing_ar   = 2 × deposit_ar   (AR doubled — 100% interest accrued)
withdraw_counted_capacity = u64::MAX × 2 = 2^65 - 2
as u64 result    = u64::MAX - 1   (lowest 64 bits)
```

In this case `safe_add(occupied_capacity)` succeeds, returning `Ok(u64::MAX - 1 + occupied_capacity)` — a value far below the correct `2 × u64::MAX`. The function returns `Ok` with a silently wrong result, and no error is ever raised. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L202-204)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/src/lib.rs (L242-245)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/dao/src/lib.rs (L256-258)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/src/lib.rs (L312-333)
```rust
    fn withdrawed_interests(
        &self,
        mut rtxs: impl Iterator<Item = &'a ResolvedTransaction> + Clone,
    ) -> Result<Capacity, DaoError> {
        let maximum_withdraws = rtxs.clone().try_fold(Capacity::zero(), |capacities, rtx| {
            self.transaction_maximum_withdraw(rtx)
                .and_then(|c| capacities.safe_add(c).map_err(Into::into))
        })?;
        let input_capacities = rtxs.try_fold(Capacity::zero(), |capacities, rtx| {
            let tx_input_capacities = rtx.resolved_inputs.iter().try_fold(
                Capacity::zero(),
                |tx_capacities, cell_meta| {
                    let output_capacity: Capacity = cell_meta.cell_output.capacity().into();
                    tx_capacities.safe_add(output_capacity)
                },
            )?;
            capacities.safe_add(tx_input_capacities)
        })?;
        maximum_withdraws
            .safe_sub(input_capacities)
            .map_err(Into::into)
    }
```

**File:** util/dao/src/tests.rs (L295-350)
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
}
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
