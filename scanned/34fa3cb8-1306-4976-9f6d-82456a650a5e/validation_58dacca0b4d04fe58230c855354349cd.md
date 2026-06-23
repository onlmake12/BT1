### Title
Silent u128→u64 Truncation in DAO Withdrawal Capacity Calculation Produces Incorrect Withdrawal Amounts — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes `withdraw_counted_capacity` as a `u128` intermediate value but converts it to `u64` with a bare `as u64` cast — a silent truncating cast. Every other analogous u128→u64 narrowing in the same codebase uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. When the u128 result exceeds `u64::MAX`, the high 64 bits are silently discarded, producing a drastically underestimated withdrawal capacity that propagates into the on-chain DAO field (`S_i`) committed by block producers.

---

### Finding Description

In `DaoCalculator::calculate_maximum_withdraw`:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast silently discards the upper 64 bits of `withdraw_counted_capacity` whenever it exceeds `u64::MAX`. No error is returned; the function continues with a wrong (too-low) value.

**Contrast with the safe pattern used everywhere else in the same file:**

`miner_issuance128` conversion: [2](#0-1) 

`ar_increase128` conversion: [3](#0-2) 

`secondary_block_reward` conversion: [4](#0-3) 

All three use `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. The `calculate_maximum_withdraw` function is the sole exception.

**Call chain into consensus-critical code:**

`calculate_maximum_withdraw` ← `transaction_maximum_withdraw` ← `withdrawed_interests` ← `dao_field_with_current_epoch` [5](#0-4) [6](#0-5) 

The DAO field `current_s` is computed as:

```
current_s = parent_s + nervosdao_issuance - withdrawed_interests
```

If `withdrawed_interests` is underestimated (because `withdraw_counted_capacity` was truncated), `current_s` is inflated — creating phantom secondary issuance that future DAO depositors can claim.

**Why the existing overflow test does not cover this path:**

The test `check_withdraw_calculation_overflows` uses a capacity near `u64::MAX` with a small `ar` ratio. In that case, `withdraw_counted_capacity` stays below `u64::MAX` (no truncation), and the error is caught by the subsequent `safe_add` when adding `occupied_capacity`. The silent truncation path — where `withdraw_counted_capacity > u64::MAX` but `(truncated_value + occupied_capacity) <= u64::MAX` — is untested and returns `Ok` with a wrong value. [7](#0-6) 

---

### Impact Explanation

When `withdraw_counted_capacity` overflows `u64`, the `as u64` cast wraps it to a small value. The function returns `Ok(wrong_capacity)` instead of `Err(Overflow)`. This wrong capacity flows into `withdrawed_interests`, which is subtracted from `current_s` in the DAO field. An underestimated `withdrawed_interests` inflates `S_i` in the committed DAO field, allowing future DAO depositors to withdraw more than the protocol should issue — effectively creating CKB value from nothing. Additionally, the DAO depositor whose withdrawal triggered the bug receives less than their entitled amount.

The `CapacityVerifier` explicitly skips the `OutputsSumOverflow` check for DAO withdrawal transactions, so a transaction with a truncated (too-low) output capacity passes verification without error: [8](#0-7) 

---

### Likelihood Explanation

The trigger condition is:

```
counted_capacity × withdrawing_ar / deposit_ar > u64::MAX
```

`counted_capacity` is bounded by the total CKB supply (~3.36 × 10¹⁸ shannons). For the product to exceed `u64::MAX` (~1.84 × 10¹⁹), the `ar` ratio `withdrawing_ar / deposit_ar` must exceed approximately 5.5×. At CKB's secondary issuance rate (~3.36% annually), this requires roughly 50+ years of elapsed time between deposit and withdrawal for a maximum-size deposit. Likelihood is **low in the near term** but increases monotonically as the chain ages and `ar` grows. The bug is latent and deterministic — all nodes compute the same wrong value, so there is no consensus split, but the economic damage accumulates silently.

---

### Recommendation

Replace the bare `as u64` cast with a checked conversion, consistent with every other u128→u64 narrowing in the file:

```rust
// Before (silent truncation):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (checked, consistent with the rest of the file):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?
``` [9](#0-8) 

---

### Proof of Concept

The following values demonstrate the silent truncation path (no error returned, wrong capacity produced):

```
deposit_ar      = 10_000_000_000_000_000   (10^16, genesis value)
withdrawing_ar  = 60_000_000_000_000_000   (6× growth, ~50+ years)
counted_capacity = 3_360_000_000_000_000_000  (total CKB supply in shannons)

withdraw_counted_capacity (u128) =
    3_360_000_000_000_000_000 × 60_000_000_000_000_000
    / 10_000_000_000_000_000
  = 20_160_000_000_000_000_000   ← exceeds u64::MAX (18_446_744_073_709_551_615)

withdraw_counted_capacity as u64 =
    20_160_000_000_000_000_000 mod 2^64
  = 1_713_255_926_290_448_384   ← drastically wrong (truncated)

withdraw_capacity = 1_713_255_926_290_448_384 + occupied_capacity
                  ← Ok(...) returned, no error, wrong value committed
```

The correct behavior (matching all other u128→u64 conversions in the file) would be to return `Err(DaoError::Overflow)`.

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

**File:** util/dao/src/lib.rs (L208-254)
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

**File:** verification/src/transaction_verifier.rs (L483-493)
```rust
        if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
            let inputs_sum = self.resolved_transaction.inputs_capacity()?;
            let outputs_sum = self.resolved_transaction.outputs_capacity()?;

            if inputs_sum < outputs_sum {
                return Err((TransactionError::OutputsSumOverflow {
                    inputs_sum,
                    outputs_sum,
                })
                .into());
            }
```
