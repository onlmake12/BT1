### Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Bypasses Overflow Guard - (File: `util/dao/src/lib.rs`)

### Summary
`DaoCalculator::calculate_maximum_withdraw` computes a u128 intermediate value and casts it to u64 with a bare `as u64` (silent truncation), while every analogous u128→u64 narrowing in the same file uses `u64::try_from(…).map_err(|_| DaoError::Overflow)?`. If the intermediate value exceeds `u64::MAX`, the high bits are silently discarded, producing a wrong (too-small) withdrawal capacity that propagates into both the `calculate_dao_maximum_withdraw` RPC response and the consensus-critical DAO field stored in block headers.

### Finding Description
In `util/dao/src/lib.rs` lines 152–156:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
```

The cast `withdraw_counted_capacity as u64` silently discards the upper 64 bits when the value exceeds `u64::MAX`.

Compare with the two analogous calculations in the same file that correctly use checked conversion:

- `secondary_block_reward` (line 204): `u64::try_from(reward128).map_err(|_| DaoError::Overflow)?`
- `dao_field_with_current_epoch` (line 245): `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?`

The `Capacity` safe-math library (`safe_add`, `safe_sub`, `safe_mul`, `safe_mul_ratio` in `util/occupied-capacity/core/src/units.rs`) correctly uses `checked_*` Rust primitives throughout, but the narrowing step before `Capacity::shannons(…)` is the unguarded gap.

The `calculate_maximum_withdraw` function is called from two production paths:
1. `transaction_maximum_withdraw` → `withdrawed_interests` → `dao_field_with_current_epoch` — the DAO field written into every block header is consensus-critical.
2. `ExperimentRpcImpl::calculate_dao_maximum_withdraw` — the public RPC endpoint reachable by any unprivileged caller.

### Impact Explanation
If `withdraw_counted_capacity` silently wraps, two outcomes are possible:

- **Truncated value + `occupied_capacity` still fits in u64**: `safe_add` succeeds and returns a wrong (too-small) capacity. The DAO field's `s` accumulator is computed with an under-counted `withdrawed_interests`, corrupting the consensus-critical DAO state stored in the block header. Nodes that independently recompute the DAO field will disagree, causing a chain split.
- **Truncated value + `occupied_capacity` overflows u64**: `safe_add` returns `DaoError::Overflow`, which is the correct error but for the wrong reason (the real overflow was silently hidden).

The existing test `check_withdraw_calculation_overflows` (lines 296–350 of `util/dao/src/tests.rs`) only exercises the `safe_add` overflow path; it does not test the case where `withdraw_counted_capacity` itself exceeds `u64::MAX` before the cast.

### Likelihood Explanation
The overflow condition requires `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. Because `counted_capacity` is bounded by the total CKB supply (~3.36 × 10¹⁸ shannons, roughly 18 % of `u64::MAX`) and the AR ratio grows slowly, this is not reachable on mainnet today. However:

- The defect is a real, latent code inconsistency — the same file applies the safe pattern everywhere else.
- The `calculate_dao_maximum_withdraw` RPC is callable by any unprivileged user with arbitrary `out_point` and `withdrawing_header_hash` inputs, making the code path fully reachable.
- The absence of a unit test specifically for the `as u64` narrowing means the defect would survive any future refactor that changes the AR growth rate or capacity limits.

### Recommendation
Replace the silent cast with the same checked conversion used elsewhere in the file:

```rust
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
```

Add a dedicated unit test that constructs a scenario where `counted_capacity * withdrawing_ar / deposit_ar` exceeds `u64::MAX` and asserts `DaoError::Overflow` is returned, analogous to `check_dao_data_calculation_overflows` for `dao_field_with_current_epoch`.

### Proof of Concept

The inconsistency is directly visible by comparing the three u128→u64 narrowings in `util/dao/src/lib.rs`:

**Unguarded (line 156):** [1](#0-0) 

**Guarded — `secondary_block_reward` (line 204):** [2](#0-1) 

**Guarded — `dao_field_with_current_epoch` (line 244–245):** [3](#0-2) 

The `Capacity` safe-math library that the rest of the codebase relies on: [4](#0-3) 

The existing overflow test that does **not** cover the `as u64` truncation path: [5](#0-4) 

The public RPC entry point that makes this code reachable by any unprivileged caller: [6](#0-5)

### Citations

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L202-205)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
        Ok(Capacity::shannons(reward))
```

**File:** util/dao/src/lib.rs (L242-245)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/occupied-capacity/core/src/units.rs (L124-155)
```rust
    /// Adds self and rhs and checks overflow error.
    pub fn safe_add<C: IntoCapacity>(self, rhs: C) -> Result<Self> {
        self.0
            .checked_add(rhs.into_capacity().0)
            .map(Capacity::shannons)
            .ok_or(Error::Overflow)
    }

    /// Subtracts self and rhs and checks overflow error.
    pub fn safe_sub<C: IntoCapacity>(self, rhs: C) -> Result<Self> {
        self.0
            .checked_sub(rhs.into_capacity().0)
            .map(Capacity::shannons)
            .ok_or(Error::Overflow)
    }

    /// Multiplies self and rhs and checks overflow error.
    pub fn safe_mul<C: IntoCapacity>(self, rhs: C) -> Result<Self> {
        self.0
            .checked_mul(rhs.into_capacity().0)
            .map(Capacity::shannons)
            .ok_or(Error::Overflow)
    }

    /// Multiplies self with a ratio and checks overflow error.
    pub fn safe_mul_ratio(self, ratio: Ratio) -> Result<Self> {
        self.0
            .checked_mul(ratio.numer())
            .and_then(|ret| ret.checked_div(ratio.denom()))
            .map(Capacity::shannons)
            .ok_or(Error::Overflow)
    }
```

**File:** util/dao/src/tests.rs (L296-350)
```rust
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
