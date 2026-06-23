### Title
Silent u128→u64 Truncating Cast in NervosDAO Withdrawal Capacity Calculation - (File: util/dao/src/lib.rs)

### Summary

`DaoCalculator::calculate_maximum_withdraw` uses a bare `as u64` truncating cast on a `u128` intermediate result, while every other analogous u128→u64 conversion in the same file uses the checked `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern. If the intermediate value exceeds `u64::MAX`, the function silently returns a wrong (bit-truncated) withdrawal capacity instead of propagating an error, causing the NervosDAO script to reject the resulting withdrawal transaction.

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the maximum withdrawable capacity as:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← truncating cast
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast silently discards the upper 64 bits when `withdraw_counted_capacity > u64::MAX`. Every other u128→u64 narrowing in the same file uses the checked form:

```rust
// secondary_block_reward (line 204)
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// dao_field_with_current_epoch (line 245, 258)
u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) 

The overflow condition is:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

Since `withdrawing_ar ≥ deposit_ar`, the ratio is ≥ 1. Overflow requires the accumulated-rate ratio to grow large enough that the interest-adjusted capacity exceeds `u64::MAX ≈ 1.84 × 10^19` shannons.

### Impact Explanation

When the truncating cast fires, `withdraw_counted_capacity as u64` wraps to a small value. `safe_add(occupied_capacity)` then succeeds (no error), and the function returns a silently incorrect (far too small) `Capacity`. Any wallet or RPC caller using this value to construct a withdrawal transaction will produce an output whose capacity does not match what the NervosDAO script independently computes, causing the transaction to be rejected with an `Overflow` script error. The depositor cannot withdraw their locked CKB.

The function is exposed via the `calculate_dao_maximum_withdraw` RPC endpoint: [4](#0-3) 

The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` asserts `result.is_err()`, but it exercises the `safe_add` overflow path (where `withdraw_counted_capacity` is still within u64 range but adding `occupied_capacity` overflows), not the `as u64` truncation path. A scenario where `withdraw_counted_capacity` itself exceeds u64 would silently return `Ok(wrong_value)`, contradicting the test's intent. [5](#0-4) 

### Likelihood Explanation

The `ar` (accumulated rate) field starts at `10^10` on mainnet and grows proportionally to the secondary issuance rate (~0.5% per year of total CKB supply). For the ratio `withdrawing_ar / deposit_ar` to be large enough to push a realistic `counted_capacity` over `u64::MAX`, the chain would need to operate for centuries. Under current economic parameters this is not reachable in the near term, making the likelihood very low. The finding is reported because the code inconsistency is real, the silent-truncation behavior is demonstrably wrong relative to the rest of the file, and the impact (blocked withdrawal, wrong capacity) is concrete if the condition is ever met.

### Recommendation

Replace the truncating cast with the same checked conversion used everywhere else in the file:

```rust
let withdraw_capacity = Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
).safe_add(occupied_capacity)?;
``` [6](#0-5) 

### Proof of Concept

Set `deposit_ar = D`, `withdrawing_ar = W` with `W/D > 1`, and `counted_capacity = C` such that `C × W / D > u64::MAX`. With the current `as u64` cast, `calculate_maximum_withdraw` returns `Ok(Capacity::shannons(truncated_value))` instead of `Err(DaoError::Overflow)`. A withdrawal transaction built from this value is rejected by the NervosDAO script, which computes the correct value independently. The depositor's funds are inaccessible until the bug is fixed.

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

**File:** util/dao/src/lib.rs (L242-258)
```rust
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
```

**File:** rpc/src/module/experiment.rs (L1-2)
```rust
use crate::error::RPCError;
use crate::module::chain::CyclesEstimator;
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
