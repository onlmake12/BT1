### Title
Silent `u128`→`u64` Truncation in `calculate_maximum_withdraw` Produces Incorrect DAO Withdrawal Capacity — (File: `util/dao/src/lib.rs`)

### Summary
`DaoCalculator::calculate_maximum_withdraw` computes the DAO withdrawal capacity using a `u128` intermediate value but converts it to `u64` via a silent truncating `as u64` cast. Every other analogous `u128`→`u64` conversion in the same file uses the checked `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern. When the intermediate result exceeds `u64::MAX`, the capacity is silently truncated to a wrong (much smaller) value, producing an incorrect withdrawal amount and corrupting downstream accounting.

### Finding Description
In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `withdraw_counted_capacity as u64` cast on line 156 silently discards the upper 64 bits if the value exceeds `u64::MAX`. This is inconsistent with every other `u128`→`u64` conversion in the same file, all of which use the checked form:

- `secondary_block_reward`: `let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;` [2](#0-1) 
- `dao_field_with_current_epoch` (miner issuance): `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?` [3](#0-2) 
- `dao_field_with_current_epoch` (AR increase): `let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;` [4](#0-3) 

The formula is `counted_capacity × withdrawing_ar / deposit_ar`. Since `withdrawing_ar ≥ deposit_ar` always holds (the accumulate rate `ar` is monotonically non-decreasing), the result is always ≥ `counted_capacity`. If `counted_capacity` is large and the AR ratio has grown sufficiently, the result overflows `u64` and is silently wrapped to a small incorrect value.

`calculate_maximum_withdraw` is called from three paths:
1. `transaction_maximum_withdraw` → `transaction_fee` (used in tx-pool admission and block verification) [5](#0-4) 
2. `transaction_maximum_withdraw` → `withdrawed_interests` → `dao_field_with_current_epoch` (DAO state field update) [6](#0-5) 
3. Directly from the RPC `calculate_dao_maximum_withdraw` [7](#0-6) 

### Impact Explanation
When truncation occurs, `withdraw_capacity` is computed as a much smaller value than the true maximum withdrawal. This has two concrete effects:

1. **Incorrect transaction fee**: `transaction_fee = maximum_withdraw - outputs_capacity`. If `maximum_withdraw` is truncated to a small value while `outputs_capacity` is the legitimate withdrawal amount, `safe_sub` returns `Err(Overflow)`, causing a legitimate DAO withdrawal transaction to be incorrectly rejected at the tx-pool or block verification layer. [5](#0-4) 

2. **Corrupted DAO state field**: `withdrawed_interests` uses `transaction_maximum_withdraw` to compute the interest withdrawn from the NervosDAO pool. A truncated value causes `current_s` (the NervosDAO secondary issuance accumulator) to be updated with a wrong (too large) subtraction, permanently corrupting the on-chain DAO state field for all subsequent blocks. [8](#0-7) 

### Likelihood Explanation
The truncation requires `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX ≈ 1.84 × 10^19`. Since `counted_capacity` is bounded by the total CKB supply (~3.36 × 10^18 shannons at genesis, growing slowly), the AR ratio would need to grow by a factor of ~5.5× from deposit to withdrawal. Given the secondary issuance rate (~2–3% per year of total supply), this corresponds to a multi-decade time horizon. Likelihood is **low** in the near term but the inconsistency with the rest of the file is a clear correctness defect that will become exploitable as the chain matures.

### Recommendation
Replace the silent truncating cast with the checked conversion used consistently elsewhere in the file:

```rust
// Before (line 155-156):
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After:
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    ).safe_add(occupied_capacity)?;
``` [9](#0-8) 

### Proof of Concept
The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` already exercises a near-overflow scenario and expects `is_err()`. [10](#0-9)  However, it relies on `safe_add` catching the overflow after the truncation, not on the truncation itself being caught. A crafted test with `deposit_ar = 1` and `withdrawing_ar = u64::MAX` and `counted_capacity = 2` would produce `withdraw_counted_capacity = 2 × u64::MAX` (a valid `u128`), which `as u64` silently truncates to `u64::MAX - 1` (wrong), while `u64::try_from` would correctly return `Err(Overflow)`.

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

**File:** util/dao/src/lib.rs (L252-254)
```rust
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;
```

**File:** util/dao/src/lib.rs (L258-258)
```rust
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

**File:** rpc/src/module/experiment.rs (L259-267)
```rust
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
