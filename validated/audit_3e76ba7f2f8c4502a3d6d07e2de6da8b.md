### Title
Silent `u128 as u64` Truncation in `calculate_maximum_withdraw` Produces Wrong DAO Withdrawal Amounts Without Error — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` uses a bare `as u64` cast to narrow a `u128` intermediate result, silently truncating on overflow. Every other `u128 → u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. The inconsistency means that when the intermediate product overflows `u64::MAX`, the function silently returns a drastically smaller (wrong) withdrawal capacity instead of propagating an `Overflow` error, causing a DAO depositor to receive far less than their entitled amount with no indication of failure.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the withdrawable capacity as:

```rust
// lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast is a **wrapping/truncating** cast in Rust. If `withdraw_counted_capacity > u64::MAX`, the result is `withdraw_counted_capacity % 2^64`, which can be arbitrarily small (including zero). The subsequent `safe_add(occupied_capacity)` may then succeed (no overflow), so the function returns `Ok(occupied_capacity)` — the bare minimum cell overhead — instead of the correct large withdrawal amount.

Compare with the three other `u128 → u64` narrowings in the same file, all of which use checked conversion:

```rust
// line 204 (secondary_block_reward)
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// line 245 (dao_field_with_current_epoch — miner_issuance)
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)

// line 258 (dao_field_with_current_epoch — ar_increase)
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) 

`calculate_maximum_withdraw` is the sole outlier.

The existing overflow test (`check_withdraw_calculation_overflows`) does **not** exercise the `as u64` truncation path. In that test, `withdraw_counted_capacity ≈ 1.845 × 10^19 < u64::MAX`, so the `as u64` cast does not truncate; the error is raised later by `safe_add` when adding `occupied_capacity` pushes the sum past `u64::MAX`. The test therefore passes for the wrong reason and provides no coverage of the silent-truncation branch. [4](#0-3) 

---

### Impact Explanation

When `withdraw_counted_capacity` wraps to a small value, `calculate_maximum_withdraw` returns `Ok(small_wrong_capacity)` instead of `Err(Overflow)`. This propagates through:

1. **`transaction_maximum_withdraw` → `transaction_fee`** — the fee is computed as `maximum_withdraw - outputs_capacity`. A silently-truncated `maximum_withdraw` makes the fee appear negative (underflow → `DaoError`), causing a legitimate withdrawal to be rejected, or — if the truncated value happens to be larger than `outputs_capacity` — allows the transaction to pass fee validation with a wrong fee, potentially enabling a depositor to claim more than entitled.

2. **`calculate_dao_maximum_withdraw` RPC** — returns a silently wrong estimate to the caller, misleading wallet software about the actual withdrawal amount. [5](#0-4) [6](#0-5) 

---

### Likelihood Explanation

The overflow condition requires `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`. Because `counted_capacity` is bounded by the total CKB supply (~33.6 billion CKB = ~3.36 × 10^18 shannons, well below `u64::MAX ≈ 1.84 × 10^19` shannons), and `ar` grows slowly, the overflow is not reachable under current supply constraints. However:

- The `ar` accumulation rate is unbounded over time; if it ever doubles relative to a deposit's `deposit_ar`, any deposit exceeding `u64::MAX / 2` shannons would trigger the truncation.
- The code is already inconsistent with its own safety pattern, making it a latent correctness defect that will become exploitable as the network matures.
- The test suite does not cover the truncation path, so the defect is invisible to CI.

---

### Recommendation

Replace the bare `as u64` cast with the same checked pattern used everywhere else in the file:

```rust
// Before (line 155-156):
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After:
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
```

Add a dedicated unit test that constructs `deposit_ar` and `withdrawing_ar` values such that `counted_capacity × withdrawing_ar / deposit_ar` strictly exceeds `u64::MAX` while `safe_add` would not independently overflow, and asserts that the function returns `Err(DaoError::Overflow)`.

---

### Proof of Concept

Construct inputs where `withdraw_counted_capacity` falls in the range `(u64::MAX, u64::MAX + occupied_capacity)` so that `as u64` wraps to a small value but `safe_add` succeeds:

```
deposit_ar      = 10_000_000_000_000_000   (10^16, genesis value)
withdrawing_ar  = 20_000_000_000_000_000   (2 × deposit_ar, ar doubled)
output.capacity = 18_446_744_073_709_551_615  (u64::MAX shannons)
occupied_capacity ≈ 4_100_000_000 shannons (41-byte cell)

counted_capacity = u64::MAX - 4_100_000_000 ≈ 18_446_744_069_609_551_615

withdraw_counted_capacity (u128)
  = 18_446_744_069_609_551_615 × 2 / 1
  = 36_893_488_139_219_103_230
  > u64::MAX (18_446_744_073_709_551_615)

withdraw_counted_capacity as u64
  = 36_893_488_139_219_103_230 % 2^64
  = 36_893_488_139_219_103_230 - 18_446_744_073_709_551_616
  = 18_446_744_065_509_551_614   ← wrong, much smaller than correct value

withdraw_capacity = 18_446_744_065_509_551_614 + 4_100_000_000
                  = 18_446_744_069_609_551_614   ← Ok(wrong), not Err(Overflow)
```

With the correct `u64::try_from` check, the function would return `Err(DaoError::Overflow)` at the narrowing step, consistent with the behavior of `secondary_block_reward` and `dao_field_with_current_epoch` under analogous overflow conditions. [7](#0-6) [8](#0-7)

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

**File:** util/dao/src/lib.rs (L126-159)
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
    }
```

**File:** util/dao/src/lib.rs (L202-205)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
        Ok(Capacity::shannons(reward))
```

**File:** util/dao/src/lib.rs (L244-258)
```rust
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
        let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;

        let current_c = parent_c.safe_add(current_g)?;
        let current_u = parent_u
            .safe_add(added_occupied_capacities)
            .and_then(|u| u.safe_sub(freed_occupied_capacities))?;
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;

        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
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

**File:** util/dao/utils/src/error.rs (L36-38)
```rust
    /// Calculation overflow
    #[error("Overflow")]
    Overflow,
```
