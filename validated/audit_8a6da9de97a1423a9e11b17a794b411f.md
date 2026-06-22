### Title
Silent Truncating Cast from `u128` to `u64` in DAO Withdrawal Calculation Produces Wrong Capacity — (`File: util/dao/src/lib.rs`)

---

### Summary

In `util/dao/src/lib.rs`, the function `calculate_maximum_withdraw` computes the DAO interest-adjusted withdrawal amount using a `u128` intermediate value, then silently truncates it to `u64` via an unchecked `as u64` cast. If the intermediate product overflows `u64::MAX`, the result is silently wrapped to a much smaller value. Every other analogous `u128 → u64` narrowing in the same file uses the safe `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern — making this a clear, isolated inconsistency.

---

### Finding Description

In `calculate_maximum_withdraw`, the interest-adjusted withdrawal capacity is computed as:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The expression `withdraw_counted_capacity as u64` is a Rust truncating cast: if `withdraw_counted_capacity > u64::MAX`, the upper 64 bits are silently discarded and the result wraps to a small, incorrect value. No error is returned and no panic occurs.

By contrast, every other `u128 → u64` narrowing in the same file uses the safe pattern:

```rust
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The three safe conversions are at lines 204, 245, and 258; the unsafe one is at line 156.

The `calculate_maximum_withdraw` function is called from two paths:

1. **`transaction_maximum_withdraw`** → **`withdrawed_interests`** → **`dao_field_with_current_epoch`**: the result feeds directly into the `current_s` (DAO savings) field written into every new block header's DAO data. [5](#0-4) 

2. **`transaction_fee`**: used by the tx-pool to compute the fee for DAO withdrawal transactions. [6](#0-5) 

---

### Impact Explanation

If `withdraw_counted_capacity` overflows `u64::MAX`, the `as u64` cast silently produces a value that is `withdraw_counted_capacity mod 2^64` — potentially orders of magnitude smaller than the correct value.

**Path 1 — DAO field corruption:** `withdrawed_interests` sums the (now wrong) maximum-withdraw values for all DAO withdrawal transactions in a block. This sum is subtracted from `parent_s` to produce `current_s`:

```rust
let current_s = parent_s
    .safe_add(nervosdao_issuance)
    .and_then(|s| s.safe_sub(withdrawed_interests))?;
``` [7](#0-6) 

A truncated (too-small) `withdrawed_interests` causes `current_s` to be inflated. This corrupted `S` field propagates into all subsequent block headers and affects every future DAO interest calculation, including the AR accumulation rate.

**Path 2 — Fee miscalculation:** `transaction_fee` returns `maximum_withdraw - outputs_capacity`. A truncated `maximum_withdraw` can make this subtraction underflow, causing `safe_sub` to return a `CapacityError::Overflow`, which would cause the tx-pool to reject a valid DAO withdrawal transaction. [6](#0-5) 

---

### Likelihood Explanation

For overflow to occur: `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX ≈ 1.84 × 10¹⁹`.

- `deposit_ar` starts at `10_000_000_000` (10¹⁰) at genesis and only increases.
- `counted_capacity` is bounded by the total CKB supply (~3.36 × 10¹⁸ shannons).
- Overflow requires `withdrawing_ar / deposit_ar > ~5.5×`, meaning the AR must grow more than 5.5× from the deposit block to the withdrawal block.

Under normal secondary issuance rates this ratio grows slowly, making overflow unlikely in the near term on mainnet. However:

- The bug is latent and grows more likely as the chain ages.
- The inconsistency with the three safe `try_from` conversions in the same function body is a clear code defect regardless of current likelihood.
- Any future change to issuance parameters or a long-lived deposit could bring the ratio into overflow range.

---

### Recommendation

Replace the unchecked cast with the same safe pattern used elsewhere in the file:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (safe):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?
```

This is consistent with lines 204, 245, and 258 of the same file.

---

### Proof of Concept

The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` already demonstrates that the overflow path is reachable: [8](#0-7) 

That test uses a near-`u64::MAX` output capacity and expects an error — but the error it catches comes from `safe_sub` on `counted_capacity`, not from the `as u64` cast. A crafted scenario where `counted_capacity` is large and `withdrawing_ar / deposit_ar > 1` (achievable after sufficient chain time) would reach the `as u64` cast and silently truncate, returning `Ok(wrong_capacity)` instead of `Err(DaoError::Overflow)`.

Concretely: a DAO depositor with a cell holding the maximum practical capacity (~3.36 × 10¹⁸ shannons) who withdraws after the AR has grown by a factor of ~5.5× would trigger the silent truncation. The resulting wrong `withdraw_capacity` would be accepted by `calculate_maximum_withdraw`, propagate through `withdrawed_interests`, and corrupt the `S` field in the block header's DAO data for every block that includes such a withdrawal transaction.

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

**File:** util/dao/src/tests.rs (L295-349)
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
```
