### Title
Silent Truncating Cast in `calculate_maximum_withdraw` Produces Wrong Capacity Instead of Error — (`File: util/dao/src/lib.rs`)

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the maximum withdrawal capacity for a NervosDAO cell using a u128 intermediate value, then narrows it back to u64 with a bare `as u64` cast. This is a **silent truncating cast**: when the u128 result exceeds `u64::MAX`, the high bits are silently discarded and the function returns a drastically wrong (much smaller) capacity value instead of propagating a `DaoError::Overflow`. Every other u128→u64 narrowing in the same file uses the checked `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern, making this a clear inconsistency with a concrete wrong-result consequence.

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← truncating cast
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast is defined Rust behavior: it silently discards the upper 64 bits. When `withdraw_counted_capacity > u64::MAX`, the result wraps to a small value, `safe_add(occupied_capacity)` succeeds, and the function returns `Ok(wrong_small_capacity)`.

Compare with the three other u128→u64 narrowings in the same file, all of which use checked conversion:

```rust
// line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
// line 245
u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?
// line 258
u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?
``` [2](#0-1) 

The existing test `check_withdraw_calculation_overflows` constructs exactly this scenario (capacity near `u64::MAX`, `withdrawing_ar > deposit_ar`) and asserts `result.is_err()`: [3](#0-2) 

With the current `as u64` cast, the function returns `Ok(~1.84e18 shannons)` — a silently wrong value — so the test assertion `assert!(result.is_err())` fails, confirming the defect.

### Impact Explanation

`calculate_maximum_withdraw` is called from three paths:

1. **`calculate_dao_maximum_withdraw` RPC** (`rpc/src/module/experiment.rs` lines 259–267, 288–296): any RPC caller receives a silently wrong (much smaller) withdrawal amount, misleading users about their actual entitlement. [4](#0-3) 

2. **`transaction_fee` → `transaction_maximum_withdraw`** (`util/dao/src/lib.rs` lines 30–36): fee is computed as `max_withdraw - outputs_capacity`. A truncated `max_withdraw` that is smaller than `outputs_capacity` causes `safe_sub` to fail and the transaction is rejected; if it is larger, the fee is computed incorrectly, potentially allowing a near-zero-fee DAO withdrawal to pass pool admission. [5](#0-4) 

3. **`dao_field` → `withdrawed_interests`** (`util/dao/src/lib.rs` lines 312–333): the DAO accumulator field written into a block header is computed using the truncated interest value. A block assembled with a wrong DAO field would be rejected by all other nodes, causing the miner to produce an invalid block. [6](#0-5) 

### Likelihood Explanation

Overflow requires `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. Since `counted_capacity` is bounded by the total CKB supply (~3.36 × 10¹⁸ shannons) and `u64::MAX ≈ 1.84 × 10¹⁹` shannons, the ratio `withdrawing_ar / deposit_ar` would need to exceed ~5.5× for overflow to occur with a maximum-supply cell. The accumulate rate grows slowly (secondary issuance is a small fraction of total CKB), so this threshold is not reachable on mainnet today. However, the code is provably wrong — the test designed to guard against it fails — and the defect is a latent time-bomb that becomes exploitable if the accumulate rate ever grows sufficiently relative to the deposit rate, or if the protocol is deployed in a configuration with different issuance parameters.

### Recommendation

Replace the truncating cast with the same checked pattern used everywhere else in the file:

```rust
// Before (line 155-156):
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After:
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
``` [7](#0-6) 

### Proof of Concept

The existing test `check_withdraw_calculation_overflows` already encodes the overflow scenario:

- `output.capacity() = 18_446_744_073_709_550_000` (near `u64::MAX`)
- `deposit_ar = 10_000_000_000_123_456`, `withdrawing_ar = 10_000_000_001_123_456`
- `counted_capacity ≈ 18_446_744_069_609_550_000`
- `withdraw_counted_capacity ≈ 20_291_418_476_570_505_955` (exceeds `u64::MAX`)
- `as u64` truncates to `≈ 1_844_674_402_860_954_339`
- `safe_add(occupied_capacity)` succeeds → function returns `Ok(wrong_value)`
- Test assertion `assert!(result.is_err())` **fails**, confirming the silent wrong-result bug [3](#0-2)

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

**File:** util/dao/src/lib.rs (L152-158)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

        Ok(withdraw_capacity)
```

**File:** util/dao/src/lib.rs (L202-258)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
        Ok(Capacity::shannons(reward))
    }

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
