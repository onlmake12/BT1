### Title
Silent Truncating Cast in `calculate_maximum_withdraw` Produces Wrong Withdrawal Amount Instead of Error — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes `withdraw_counted_capacity` as a `u128` but casts it to `u64` with a bare `as u64` (truncating, not checked). Every other analogous `u128 → u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. When the `u128` value exceeds `u64::MAX`, the truncating cast silently discards the high bits and returns a drastically wrong (much smaller) capacity instead of propagating an error. The existing test `check_withdraw_calculation_overflows` documents that an `Err` is expected in this case, but the truncating cast means the function returns `Ok` with a corrupted value.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the interest-adjusted withdrawal capacity:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← truncating cast
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast silently truncates any bits above bit 63. Compare this to every other `u128 → u64` narrowing in the same file, all of which use the checked form:

```rust
// line 245
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)
// line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
// line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The test `check_withdraw_calculation_overflows` explicitly expects `result.is_err()` for a cell with capacity `18_446_744_073_709_550_000` shannons (near `u64::MAX`) and a positive AR ratio: [5](#0-4) [6](#0-5) 

With those inputs, `withdraw_counted_capacity` exceeds `u64::MAX`. The `as u64` cast wraps it to a small value (≈ 2 × 10¹² shannons). `safe_add(occupied_capacity)` then succeeds, and the function returns `Ok(Capacity::shannons(~2_072_599_998_384))` — roughly 10,000× smaller than the correct value — instead of `Err(DaoError::Overflow)`.

---

### Impact Explanation

`calculate_maximum_withdraw` is called in two consensus-critical paths:

1. **`withdrawed_interests` → `dao_field_with_current_epoch`**: The DAO field embedded in every block header is computed using the sum of withdrawal interests. A silently wrong (truncated) interest value corrupts `current_s` in the DAO field. Nodes that trigger the overflow condition would compute a different DAO field than nodes that do not, causing a **consensus split**.

2. **`transaction_maximum_withdraw` → `transaction_fee`**: Transaction fee validation uses the wrong withdrawal amount. A transaction that should be rejected (fee underflow) may be accepted, or vice versa, depending on the truncated value.

3. **RPC `calculate_dao_maximum_withdraw`**: Returns a wrong value to callers, who may construct transactions based on it. [7](#0-6) [8](#0-7) 

---

### Likelihood Explanation

The overflow requires `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. Since `withdrawing_ar / deposit_ar` is a ratio slightly above 1 (AR grows slowly via secondary issuance), `counted_capacity` must be very close to `u64::MAX`. The total CKB supply is ≈ 3.36 × 10¹⁸ shannons, well below `u64::MAX` (≈ 1.84 × 10¹⁹ shannons), so a single cell cannot normally hold enough capacity to trigger this today. However:

- The condition becomes reachable as AR grows over a very long chain lifetime.
- A transaction sender or block assembler who can craft a cell with capacity near the supply cap (e.g., by consolidating outputs) and reference headers with a sufficiently large AR ratio can trigger the path.
- The entry point is fully unprivileged: any P2P transaction relay, RPC `send_transaction`, or block template submission.

---

### Recommendation

Replace the truncating cast with the same checked conversion used everywhere else in the file:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    )
    .safe_add(occupied_capacity)?;
```

This makes the overflow handling consistent with `miner_issuance128`, `ar_increase128`, and `reward128` in the same file, and makes the existing test `check_withdraw_calculation_overflows` pass as intended.

---

### Proof of Concept

Given the test fixture at `util/dao/src/tests.rs:295–349`:

- `output.capacity = 18_446_744_073_709_550_000` shannons
- `deposit_ar = 10_000_000_000_123_456`, `withdrawing_ar = 10_000_000_001_123_456`
- `occupied_capacity ≈ 4_100_000_000` shannons (empty lock script)
- `counted_capacity = 18_446_744_069_609_550_000`
- `withdraw_counted_capacity = 18_446_744_069_609_550_000 × 10_000_000_001_123_456 / 10_000_000_000_123_456 ≈ 18_446_746_142_209_550_000`
- `18_446_746_142_209_550_000 > u64::MAX (18_446_744_073_709_551_615)`
- `withdraw_counted_capacity as u64 ≈ 2_068_499_998_384` (truncated)
- `Capacity::shannons(2_068_499_998_384).safe_add(4_100_000_000)` → `Ok(Capacity::shannons(2_072_599_998_384))`

The test asserts `result.is_err()` but the code returns `Ok` with a value ≈ 10,000× smaller than correct, demonstrating the silent corruption. [9](#0-8) [6](#0-5)

### Citations

**File:** util/dao/src/lib.rs (L127-159)
```rust
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

**File:** util/dao/src/lib.rs (L202-204)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/src/lib.rs (L208-264)
```rust
    /// Calculates the new dao field with specified [`EpochExt`].
    pub fn dao_field_with_current_epoch(
        &self,
        rtxs: impl Iterator<Item = &'a ResolvedTransaction> + Clone,
        parent: &HeaderView,
        current_block_epoch: &EpochExt,
    ) -> Result<Byte32, DaoError> {
        // Freed occupied capacities from consumed inputs
        let freed_occupied_capacities =
            rtxs.clone().try_fold(Capacity::zero(), |capacities, rtx| {
                self.input_occupied_capacities(rtx)
                    .and_then(|c| capacities.safe_add(c))
            })?;
        let added_occupied_capacities = self.added_occupied_capacities(rtxs.clone())?;
        let withdrawed_interests = self.withdrawed_interests(rtxs)?;

        let (parent_ar, parent_c, parent_s, parent_u) = extract_dao_data(parent.dao());

        // g contains both primary issuance and secondary issuance,
        // g2 is the secondary issuance for the block, which consists of
        // issuance for the miner, NervosDAO and treasury.
        // When calculating issuance in NervosDAO, we use the real
        // issuance for each block(which will only be issued on chain
        // after the finalization delay), not the capacities generated
        // in the cellbase of current block.
        let current_block_number = parent.number() + 1;
        let current_g2 = current_block_epoch.secondary_block_issuance(
            current_block_number,
            self.consensus.secondary_epoch_reward(),
        )?;
        let current_g = current_block_epoch
            .block_reward(current_block_number)
            .and_then(|c| c.safe_add(current_g2))?;

        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
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
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;

        Ok(pack_dao_data(current_ar, current_c, current_s, current_u))
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
