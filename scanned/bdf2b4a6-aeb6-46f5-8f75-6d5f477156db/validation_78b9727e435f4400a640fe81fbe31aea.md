### Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Produces Incorrect DAO Withdrawal Amounts — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw()` computes an intermediate `u128` withdrawal amount but casts it to `u64` with a silent truncating `as u64` cast. If the product `counted_capacity * withdrawing_ar / deposit_ar` exceeds `u64::MAX`, the high bits are silently dropped and the function returns an incorrect (too-small) withdrawal capacity with no error. This is the direct CKB analog of TRST-M-5: the `ar` accumulate-rate field is the monotonically-growing accumulator (analogous to `accRewardPerShare`), and the unchecked narrowing cast is the analog of the overflow.

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

The `withdraw_counted_capacity as u64` is a Rust truncating cast. If `withdraw_counted_capacity > u64::MAX`, the upper 64 bits are silently discarded and the function returns a value far smaller than the depositor is entitled to — with no error, no panic, and no indication that anything went wrong.

This is **directly inconsistent** with every other u128→u64 narrowing in the same file, all of which use checked conversions:

| Location | Pattern |
|---|---|
| `secondary_block_reward` | `u64::try_from(reward128).map_err(|_| DaoError::Overflow)?` |
| `dao_field_with_current_epoch` (miner issuance) | `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?` |
| `dao_field_with_current_epoch` (ar increase) | `u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?` |
| **`calculate_maximum_withdraw`** | **`withdraw_counted_capacity as u64` ← unchecked** | [2](#0-1) [3](#0-2) [4](#0-3) 

The `ar` accumulate-rate field is the CKB analog of `accRewardPerShare`. It is stored as a `u64` in the DAO field of every block header, starts at `10^16`, and grows monotonically with each block's secondary issuance:

```
ar_increase = parent_ar * g2 / C
current_ar  = parent_ar + ar_increase
``` [5](#0-4) 

It is packed and unpacked as a raw `u64` in the 32-byte DAO field:

```rust
pub fn extract_dao_data(dao: Byte32) -> (u64, Capacity, Capacity, Capacity) {
    let ar = LittleEndian::read_u64(&data[8..16]);
    ...
}
``` [6](#0-5) 

The withdrawal formula is `counted_capacity * withdrawing_ar / deposit_ar`. For this product to exceed `u64::MAX ≈ 1.84 × 10¹⁹`, the ratio `withdrawing_ar / deposit_ar` must exceed approximately 5.5 (given the total CKB supply of ~3.36 × 10¹⁸ shannons). The existing overflow test (`check_withdraw_calculation_overflows`) only exercises the `safe_add` overflow path, not the `as u64` truncation path: [7](#0-6) 

---

### Impact Explanation

When `withdraw_counted_capacity` silently truncates, `calculate_maximum_withdraw` returns a capacity far smaller than the depositor is entitled to. The DAO withdrawal transaction succeeds (no error is returned), but the depositor permanently loses the difference between the correct and truncated amounts. The NervosDAO type script enforces that the output capacity does not exceed the value returned by this function, so the depositor cannot claim the correct amount.

---

### Likelihood Explanation

Low. The `ar` accumulator grows slowly: at current mainnet rates (~3,000 shannons per block), reaching a ratio of 5.5× would take on the order of millions of years. However, the defect is a latent code inconsistency — every other u128→u64 narrowing in the same file is checked, while this one is not. The risk increases if secondary issuance parameters are ever changed or if the chain runs for an extremely long time.

---

### Recommendation

Replace the silent truncating cast with the same checked pattern used everywhere else in the file:

```rust
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    )
    .safe_add(occupied_capacity)?;
``` [8](#0-7) 

---

### Proof of Concept

The truncation is triggered when `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. A synthetic test can demonstrate the silent data loss:

```rust
// deposit_ar = 10^16 (genesis value)
// withdrawing_ar = 6 * 10^16  (ar has grown 6x)
// counted_capacity = u64::MAX / 5 ≈ 3.69 * 10^18
//
// withdraw_counted_capacity = (u64::MAX/5) * (6*10^16) / 10^16
//                           = (u64::MAX/5) * 6
//                           = 1.2 * u64::MAX  > u64::MAX
//
// `as u64` silently wraps to 0.2 * u64::MAX
// → returned capacity is 5x smaller than correct value, no error
```

The existing test infrastructure in `util/dao/src/tests.rs` already constructs synthetic `ar` values via `pack_dao_data`, making this straightforward to reproduce. [9](#0-8)

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

**File:** util/dao/src/lib.rs (L256-261)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;
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

**File:** util/dao/utils/src/lib.rs (L116-123)
```rust
pub fn pack_dao_data(ar: u64, c: Capacity, s: Capacity, u: Capacity) -> Byte32 {
    let mut buf = [0u8; 32];
    LittleEndian::write_u64(&mut buf[0..8], c.as_u64());
    LittleEndian::write_u64(&mut buf[8..16], ar);
    LittleEndian::write_u64(&mut buf[16..24], s.as_u64());
    LittleEndian::write_u64(&mut buf[24..32], u.as_u64());
    Byte32::from_slice(&buf).expect("impossible: fail to read array")
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
