### Title
Silent `u128`→`u64` Truncation in `calculate_maximum_withdraw` Skips Overflow Check — (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the interest-adjusted withdrawal capacity using a `u128` intermediate value, then casts it to `u64` with a bare `as u64` — a silent truncating cast that produces a wrong (much smaller) result instead of an error when the value exceeds `u64::MAX`. Every other analogous `u128`→`u64` conversion in the same file uses `u64::try_from(…).map_err(|_| DaoError::Overflow)?`, making this a clear inconsistency with a concrete loss-of-funds impact.

---

### Finding Description

In `calculate_maximum_withdraw`, the withdrawal amount is computed as:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `u128` multiplication itself is safe (two `u64` values multiplied together always fit in `u128`). The problem is the **`as u64` cast on line 156**, which silently wraps/truncates when `withdraw_counted_capacity > u64::MAX = 18_446_744_073_709_551_615`. The truncated value is then passed directly to `Capacity::shannons(...)`, which accepts any `u64` without complaint, so the function returns `Ok(wrong_small_value)` instead of `Err(DaoError::Overflow)`.

Compare with every other `u128`→`u64` narrowing in the same file, all of which use the checked form:

```rust
// secondary_block_reward (line 204)
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// dao_field_with_current_epoch (line 244-245)
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);

// dao_field_with_current_epoch (line 258)
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The existing test `check_withdraw_calculation_overflows` confirms the **intended** behavior is to return an error on overflow:

```rust
assert!(result.is_err());
``` [5](#0-4) 

However, the test's input cell has no lock script, so `occupied_capacity` likely fails first (at line 149), masking the `as u64` bug. A cell with a valid lock script and a large enough capacity would reach the truncating cast and silently return a wrong value.

---

### Impact Explanation

When `withdraw_counted_capacity` overflows `u64`, the `as u64` cast wraps the value modulo `2^64`. For example, if the true result is `u64::MAX + 1_844_672_791`, the cast yields `1_844_672_791` shannons (~18 CKB) instead of the correct ~184 billion CKB. The function returns `Ok(~18 CKB)`, and the DAO withdrawal transaction is validated against this silently wrong maximum — the user receives a tiny fraction of their entitled funds, with the remainder permanently locked.

---

### Likelihood Explanation

The overflow requires `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. The total CKB supply is ~33.6 billion CKB = ~3.36×10¹⁸ shannons, so `counted_capacity` is at most ~18% of `u64::MAX`. For overflow, `withdrawing_ar / deposit_ar` must exceed ~5.5×. The `ar` accumulation rate grows with secondary issuance over time; over a sufficiently long chain lifetime this ratio could be reached. More immediately, the code is provably incorrect relative to its own stated invariant (the test expects `is_err()`) and inconsistent with every other conversion in the same file.

---

### Recommendation

Replace the silent cast with the same checked conversion used everywhere else in the file:

```rust
let withdraw_capacity = Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
).safe_add(occupied_capacity)?;
``` [6](#0-5) 

---

### Proof of Concept

Using the values from the existing test:

- `output_capacity = 18_446_744_073_709_550_000` shannons
- `deposit_ar = 10_000_000_000_123_456`
- `withdrawing_ar = 10_000_000_001_123_456`
- `occupied_capacity = 0` (hypothetical cell with minimal lock script)

```
counted_capacity = 18_446_744_073_709_550_000

withdraw_counted_capacity (u128)
  = 18_446_744_073_709_550_000 × 10_000_000_001_123_456
    / 10_000_000_000_123_456
  ≈ 18_446_744_075_554_224_407          ← exceeds u64::MAX (18_446_744_073_709_551_615)

withdraw_counted_capacity as u64
  = 18_446_744_075_554_224_407 mod 2^64
  = 1_844_672_791                        ← ~18 CKB instead of ~184 billion CKB
```

The function returns `Ok(Capacity::shannons(1_844_672_791))` — a loss of essentially the entire deposit — instead of `Err(DaoError::Overflow)`. [7](#0-6)

### Citations

**File:** util/dao/src/lib.rs (L126-158)
```rust
    /// Calculate maximum withdraw capacity of a deposited dao output
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

**File:** util/dao/src/lib.rs (L256-258)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
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
