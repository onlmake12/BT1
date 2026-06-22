### Title
Silent `u128`-to-`u64` Truncation in DAO Withdrawal Capacity Calculation — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes an intermediate `u128` result (`withdraw_counted_capacity`) and then casts it to `u64` with a bare `as u64` — a **silent truncating cast** that discards the upper 64 bits without returning an error. If the `u128` value exceeds `u64::MAX`, the function silently returns a wrong (massively underestimated) withdrawal capacity instead of propagating an overflow error.

---

### Finding Description

In `util/dao/src/lib.rs`, the DAO maximum-withdraw calculation is:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The multiplication `counted_capacity * withdrawing_ar` is correctly widened to `u128` to avoid overflow. However, after the division, the result is cast back to `u64` with `as u64` — a Rust truncating cast that **never panics and never returns an error**. If `withdraw_counted_capacity > u64::MAX`, the upper bits are silently dropped, producing a value that is `withdraw_counted_capacity mod 2^64`.

The subsequent `safe_add` uses `checked_add` and would catch a second overflow, but only if the already-truncated value plus `occupied_capacity` itself overflows `u64`. If the truncated value is small (e.g., `withdraw_counted_capacity = u64::MAX + 1` → truncated to `0`), `safe_add` succeeds and the function returns a silently wrong result with no error. [2](#0-1) 

The existing test `check_withdraw_calculation_overflows` confirms the overflow concern but only exercises the path where `safe_add` catches the error — it does **not** test the silent-truncation path where `safe_add` succeeds with a wrong value: [3](#0-2) 

All other arithmetic in the same function uses `safe_sub`, `safe_add`, or `u64::try_from(...).map_err(|_| DaoError::Overflow)?` — the `as u64` cast is the sole unchecked conversion: [4](#0-3) 

---

### Impact Explanation

`calculate_maximum_withdraw` is called in two reachable paths:

1. **RPC path** — `calculate_dao_maximum_withdraw` (experiment RPC) calls it directly with caller-supplied `out_point` and block hash. A wrong return value misleads the caller about the true withdrawal amount. [5](#0-4) 

2. **Block-verification path** — `dao_field_with_current_epoch` → `withdrawed_interests` → `transaction_maximum_withdraw` → `calculate_maximum_withdraw`. A silently wrong result here corrupts the DAO field (`C`, `S`, `U`, `ar`) written into the block header, which is a consensus-critical value verified by all peers. [6](#0-5) 

If the truncated value is small enough that `safe_add(occupied_capacity)` succeeds, the node accepts a DAO withdrawal transaction with a wrong fee/interest calculation and writes a wrong DAO field into the block header. Peers that recompute the DAO field correctly would reject the block, causing a consensus split.

---

### Likelihood Explanation

The condition for silent truncation is `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. Since `counted_capacity ≤ u64::MAX` and `withdrawing_ar / deposit_ar ≥ 1`, this requires the interest multiplier to push the product above `u64::MAX`. On mainnet, CKB's total supply is ~3.36 × 10¹⁸ shannons (≈18% of `u64::MAX`), so no single cell can hold enough capacity to trigger this in practice. However:

- The `calculate_maximum_withdraw` function accepts **any** `CellOutput` and **any** two block hashes — it does not validate that the cell capacity is within realistic bounds.
- On a private or test network with different genesis parameters (e.g., large `issued_cells` capacities), the condition is reachable.
- The `ar` accumulate rate is a `u64` read directly from the block header DAO field with no upper-bound enforcement; a crafted header (e.g., relayed by a malicious peer before full verification) could supply an inflated `withdrawing_ar`. [7](#0-6) 

---

### Recommendation

Replace the bare `as u64` cast with a checked conversion that propagates `DaoError::Overflow`:

```rust
// Before (silent truncation):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After (explicit overflow check):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
```

This is consistent with how every other `u128`-to-`u64` conversion in the same file is handled: [8](#0-7) [9](#0-8) 

---

### Proof of Concept

Construct a `CellOutput` with `capacity = u64::MAX - occupied_capacity + 1` (so `counted_capacity = u64::MAX - occupied_capacity + 1 - occupied_capacity`), and supply two headers where `withdrawing_ar / deposit_ar > 1 + ε` for a small `ε` such that `counted_capacity * withdrawing_ar / deposit_ar` wraps past `u64::MAX` to a small value. The function returns `Ok(small_value + occupied_capacity)` instead of `Err(DaoError::Overflow)`.

The existing test infrastructure already constructs near-`u64::MAX` capacities: [10](#0-9) 

A variant of this test with `withdrawing_ar` set high enough to push `withdraw_counted_capacity` just past `u64::MAX` (e.g., `withdrawing_ar = deposit_ar * 2`) would demonstrate the silent-truncation path returning `Ok` with a wrong value rather than `Err`.

### Citations

**File:** util/dao/src/lib.rs (L146-158)
```rust
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

**File:** util/dao/src/lib.rs (L244-245)
```rust
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/dao/src/lib.rs (L258-261)
```rust
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;
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

**File:** util/occupied-capacity/core/src/units.rs (L125-130)
```rust
    pub fn safe_add<C: IntoCapacity>(self, rhs: C) -> Result<Self> {
        self.0
            .checked_add(rhs.into_capacity().0)
            .map(Capacity::shannons)
            .ok_or(Error::Overflow)
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

**File:** util/dao/utils/src/lib.rs (L104-111)
```rust
pub fn extract_dao_data(dao: Byte32) -> (u64, Capacity, Capacity, Capacity) {
    let data = dao.raw_data();
    let c = Capacity::shannons(LittleEndian::read_u64(&data[0..8]));
    let ar = LittleEndian::read_u64(&data[8..16]);
    let s = Capacity::shannons(LittleEndian::read_u64(&data[16..24]));
    let u = Capacity::shannons(LittleEndian::read_u64(&data[24..32]));
    (ar, c, s, u)
}
```
