### Title
Silent `u64` Truncation in `calculate_maximum_withdraw` Causes Incorrect NervosDAO Withdrawal Amounts - (File: util/dao/src/lib.rs)

---

### Summary

In `DaoCalculator::calculate_maximum_withdraw`, the intermediate `u128` result `withdraw_counted_capacity` is cast to `u64` using `as u64`, which silently truncates if the value exceeds `u64::MAX`. Every other analogous `u128`→`u64` conversion in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. The truncation can cause a NervosDAO depositor to receive a drastically wrong (much smaller) withdrawal amount, and can cause block processing to fail when such a withdrawal transaction is included in a block.

---

### Finding Description

In `DaoCalculator::calculate_maximum_withdraw`, the withdrawal amount is computed as:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `withdraw_counted_capacity as u64` cast silently truncates if `withdraw_counted_capacity > u64::MAX`. This is directly inconsistent with every other `u128`→`u64` conversion in the same file, all of which use checked conversion: [2](#0-1) [3](#0-2) [4](#0-3) 

The overflow condition is:

```
counted_capacity * withdrawing_ar / deposit_ar > u64::MAX
```

`deposit_ar` starts at `10^10`. `counted_capacity` can be up to ~3.36 × 10^18 shannons (total CKB supply). This requires `withdrawing_ar > 5.36 × 10^10`, i.e., AR must grow by ~5.36× from the time of deposit.

When truncation occurs, `withdraw_counted_capacity as u64` yields only the low 64 bits — a value far smaller than the true result. This propagates through:

1. `calculate_maximum_withdraw` returns a wrong (much smaller) value.
2. The public RPC `calculate_dao_maximum_withdraw` in `rpc/src/module/experiment.rs` calls the same function and also returns the wrong value, so the user constructs a withdrawal transaction based on the truncated amount. [5](#0-4) 

3. In `dao_field_with_current_epoch`, `withdrawed_interests = maximum_withdraws - input_capacities`. Because `maximum_withdraws` is now truncated to a value smaller than `input_capacities`, `safe_sub` returns `DaoError::Overflow`, causing block processing to fail. [6](#0-5) 

4. Even if the user sets `outputs_capacity` to the truncated value (as reported by the RPC), `transaction_fee = maximum_withdraw - outputs_capacity` computes correctly to zero, so the transaction passes fee validation — but the block that includes it is then rejected at the DAO field update step.

The existing test `check_withdraw_calculation_overflows` only exercises the `safe_add` overflow path (where `withdraw_counted_capacity` is just above `u64::MAX` after adding `occupied_capacity`), not the silent `as u64` truncation path where the truncated value is small enough that `safe_add` succeeds with a wrong result. [7](#0-6) 

---

### Impact Explanation

A NervosDAO depositor who deposited a large amount of CKB and holds until AR has grown by ~5.36× will find:

- The RPC `calculate_dao_maximum_withdraw` reports a drastically wrong (much smaller) withdrawal amount.
- Any withdrawal transaction constructed from that amount is accepted into the mempool but causes the containing block to be rejected at the `dao_field_with_current_epoch` step, because `withdrawed_interests` underflows.
- The depositor is permanently unable to withdraw their funds via the normal path, analogous to M-04 where users cannot claim rewards.
- A miner who includes such a transaction loses their block reward.

---

### Likelihood Explanation

On mainnet, the secondary epoch reward is ~1.344 billion CKB/year against a total capacity of ~33.6 billion CKB, giving an AR growth rate of ~4%/year. A 5.36× increase requires approximately 42 years of chain operation. This is a long-term scenario on mainnet. However:

- On testnets or devnets with higher secondary reward ratios or lower total capacity, the condition is reachable much sooner.
- The bug is structurally present and inconsistent with the rest of the file today.
- Any future parameter change that increases secondary issuance or reduces total capacity accelerates the timeline.

---

### Recommendation

Replace the silent `as u64` cast with the same checked conversion pattern used everywhere else in the file:

```rust
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

---

### Proof of Concept

Construct a scenario with:
- `counted_capacity = 3_360_000_000_000_000_000` shannons (~33.6 billion CKB, the total supply)
- `deposit_ar = 10_000_000_000` (initial AR)
- `withdrawing_ar = 60_000_000_000` (AR grown 6×)

Then:
```
withdraw_counted_capacity = 3_360_000_000_000_000_000 * 60_000_000_000 / 10_000_000_000
                          = 3_360_000_000_000_000_000 * 6
                          = 20_160_000_000_000_000_000
```

`u64::MAX = 18_446_744_073_709_551_615`

`20_160_000_000_000_000_000 > u64::MAX`, so:
```
withdraw_counted_capacity as u64
  = 20_160_000_000_000_000_000 - 18_446_744_073_709_551_616
  = 1_713_255_926_290_448_384
```

The user receives ~1.71 × 10^18 shannons instead of ~2.016 × 10^19 shannons — roughly 11.8× less than entitled.

Then in `dao_field_with_current_epoch`:
```
withdrawed_interests = 1_713_255_926_290_448_384 + occupied_capacity
                     - 3_360_000_000_000_000_000   (input_capacity)
```
This underflows → `safe_sub` returns `DaoError::Overflow` → block is rejected. [8](#0-7) [6](#0-5)

### Citations

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

**File:** util/dao/src/tests.rs (L296-349)
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
```
