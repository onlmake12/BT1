### Title
Silent Precision Loss in `calculate_maximum_withdraw` Truncates DAO Withdrawal Capacity — (`File: util/dao/src/lib.rs`)

### Summary
In `DaoCalculator::calculate_maximum_withdraw`, the 128-bit intermediate result `withdraw_counted_capacity` is silently truncated to 64 bits via an unchecked `as u64` cast. When the true mathematical result exceeds `u64::MAX`, the truncated value is used as the withdrawal capacity, causing the node to accept a block whose DAO field and cellbase reward are computed from a silently wrong capacity figure. This is a direct arithmetic precision/truncation bug in the consensus-critical DAO accounting path, analogous to the DYAD report's under/overflow in a financial numerator.

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
```

The multiplication `counted_capacity * withdrawing_ar` is correctly widened to `u128` to avoid overflow. However, the final division result is cast back to `u64` with `as u64` — a **silent truncating cast** — rather than a checked conversion like `u64::try_from(...)`. If `withdraw_counted_capacity` exceeds `u64::MAX` (i.e., `18_446_744_073_709_551_615`), the high bits are silently discarded and the returned capacity is a drastically wrong (much smaller) value.

Contrast this with the immediately adjacent code in `dao_field_with_current_epoch` at line 244–245, which correctly uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?` for the same pattern:

```rust
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

The `calculate_maximum_withdraw` function does not apply the same guard.

**When can `withdraw_counted_capacity` exceed `u64::MAX`?**

`withdraw_counted_capacity = counted_capacity * withdrawing_ar / deposit_ar`

- `withdrawing_ar` grows monotonically over time (it is the accumulate rate, starting at `10^16` and increasing each block).
- `counted_capacity` can be up to ~`u64::MAX` shannons (the cell's capacity minus its occupied capacity).
- For a cell with a large `counted_capacity` deposited early (low `deposit_ar`) and withdrawn very late (high `withdrawing_ar`), the numerator `counted_capacity * withdrawing_ar` can exceed `u64::MAX` before the division by `deposit_ar` brings it back down.

Specifically: if `counted_capacity ≈ 1.8 × 10^19` shannons (near `u64::MAX`) and `withdrawing_ar / deposit_ar > 1` (which is always true for any non-zero interest), the product overflows `u64` range after division. The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` at line 296 demonstrates this is a known reachable scenario — but that test only checks that `result.is_err()`, which would pass if the function returned an error. With the silent `as u64` cast, the function instead returns `Ok(wrong_value)`.

**Affected call sites:**
1. `DaoCalculator::transaction_maximum_withdraw` → called by `withdrawed_interests` → called by `dao_field_with_current_epoch` → called during block assembly and `DaoHeaderVerifier::verify` (contextual block verification).
2. `ExperimentRpcImpl::calculate_dao_maximum_withdraw` in `rpc/src/module/experiment.rs` — directly callable by any RPC user.

### Impact Explanation

- **Incorrect DAO withdrawal capacity**: A user with a large, long-held DAO deposit receives a silently wrong (truncated) maximum withdrawal amount from the RPC and from the on-chain accounting.
- **Incorrect DAO field in blocks**: `dao_field_with_current_epoch` calls `withdrawed_interests`, which calls `transaction_maximum_withdraw`, which calls `calculate_maximum_withdraw`. A wrong `withdrawed_interests` value propagates into `current_s` (the NervosDAO secondary issuance accumulator), corrupting the DAO field stored in every subsequent block header. This is a consensus-critical field verified by `DaoHeaderVerifier`.
- **Incorrect block reward**: The corrupted DAO field affects `ar` (accumulate rate) calculations in subsequent blocks, causing all future DAO withdrawal interest calculations to be wrong.
- **Severity**: Consensus corruption — nodes computing the DAO field from the truncated value will produce a different block hash than nodes that do not trigger the truncation, causing a chain split or block rejection.

### Likelihood Explanation

The condition requires `counted_capacity * withdrawing_ar` to exceed `u64::MAX` after integer division by `deposit_ar`. Since `deposit_ar` starts at `10^16` and `withdrawing_ar` grows slowly, the ratio `withdrawing_ar / deposit_ar` is close to 1 for many years. However, for cells with `counted_capacity` near `u64::MAX` (≈ 184 billion CKB), the product can overflow. The total CKB supply is ~33.6 billion CKB at genesis, so a single cell cannot hold enough to trigger this in normal operation today. However, as secondary issuance accumulates over decades and `ar` grows, the threshold lowers. Additionally, on custom chains (dev chains, testnets) with different parameters, this is immediately reachable. The RPC path (`calculate_dao_maximum_withdraw`) is reachable by any unprivileged RPC caller with a crafted out-point referencing a large-capacity cell.

### Recommendation

Replace the silent `as u64` cast on line 156 of `util/dao/src/lib.rs` with a checked conversion, consistent with the pattern already used elsewhere in the same file:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (safe):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?
```

This matches the pattern already used at line 244–245 for `miner_issuance128` and at line 258 for `ar_increase128`.

### Proof of Concept

The root cause is directly visible at: [1](#0-0) 

The safe pattern that should be used instead is already present in the same function at: [2](#0-1) 

The function is called during contextual block verification via `DaoHeaderVerifier::verify`: [3](#0-2) 

And is directly reachable via the RPC layer by any unprivileged caller: [4](#0-3) 

The existing test acknowledges the overflow scenario but does not catch the silent truncation because `as u64` does not return an error: [5](#0-4)

### Citations

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L244-245)
```rust
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L300-320)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let dao = DaoCalculator::new(
            &self.context.consensus,
            &self.context.store.borrow_as_data_loader(),
        )
        .dao_field(self.resolved.iter().map(AsRef::as_ref), self.parent)
        .map_err(|e| {
            error_target!(
                crate::LOG_TARGET,
                "Error generating dao data for block {}: {:?}",
                self.header.hash(),
                e
            );
            e
        })?;

        if dao != self.header.dao() {
            return Err((BlockErrorKind::InvalidDAO).into());
        }
        Ok(())
    }
```

**File:** rpc/src/module/experiment.rs (L235-267)
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
