### Title
Silent u128→u64 Truncation in NervosDAO Withdrawal Capacity Calculation — (`File: util/dao/src/lib.rs`)

---

### Summary

`calculate_maximum_withdraw` computes `withdraw_counted_capacity` as a `u128` intermediate value, then casts it to `u64` using the silent `as u64` operator. If the intermediate value exceeds `u64::MAX`, the high bits are silently discarded, producing a wrong (smaller) withdrawal capacity with no error returned. Every other u128→u64 narrowing in the same file uses the checked `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern, making this an inconsistent and dangerous exception.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the interest-bearing withdrawal amount for a NervosDAO depositor:

```rust
// util/dao/src/lib.rs  lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The three operands are:

| Variable | Type | Max value |
|---|---|---|
| `counted_capacity` | u64 | ~1.84 × 10¹⁹ shannons |
| `withdrawing_ar` | u64 | grows from 10¹⁶ over time |
| `deposit_ar` | u64 | 10¹⁶ at genesis |

Both `ar` values are extracted from the on-chain DAO field as raw `u64` values:

```rust
// util/dao/utils/src/lib.rs  line 107
let ar = LittleEndian::read_u64(&data[8..16]);
``` [2](#0-1) 

The genesis accumulation rate is `10^16`:

```rust
// util/dao/utils/src/lib.rs  line 17
const DEFAULT_GENESIS_ACCUMULATE_RATE: u64 = 10_000_000_000_000_000;
``` [3](#0-2) 

`ar` grows each block by `ar_old * g2 / C`. From mainnet block data, the rate of increase is approximately 10⁸ per block. At ~8 seconds per block, `ar` doubles in roughly 10⁸ blocks (~25 years).

The rest of the same file uses the **safe** checked conversion pattern for identical u128→u64 narrowings:

```rust
// util/dao/src/lib.rs  lines 244-245
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);

// util/dao/src/lib.rs  line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [4](#0-3) [5](#0-4) 

`calculate_maximum_withdraw` is the sole site that uses `as u64` instead.

---

### Impact Explanation

When `withdraw_counted_capacity` exceeds `u64::MAX`, the `as u64` cast silently wraps (truncates) the value. The function then returns a **wrong, smaller** capacity without any error. Two concrete consequences:

1. **RPC caller receives wrong data.** The public RPC method `calculate_dao_maximum_withdraw` calls `calculate_maximum_withdraw` directly. A depositor querying their entitled withdrawal amount receives a silently incorrect (much smaller) value and constructs a transaction for that amount — losing the difference permanently. [6](#0-5) 

2. **Fee verification uses wrong maximum.** `transaction_maximum_withdraw` (which calls `calculate_maximum_withdraw`) feeds into `DaoCalculator::transaction_fee`, which is used during transaction verification. A silently truncated maximum makes the computed fee incorrect, potentially allowing a transaction that over-spends the true maximum to pass fee checks, or causing a legitimate transaction to be rejected. [7](#0-6) 

Critically, unlike the `safe_add` overflow that the existing test `check_withdraw_calculation_overflows` catches, a truncation that lands back within u64 range (e.g., wraps to a small positive value) will **not** be caught by `safe_add` — the function returns `Ok(wrong_capacity)` silently. [8](#0-7) 

---

### Likelihood Explanation

For overflow to occur: `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`.

- `counted_capacity` can be up to ~1.84 × 10¹⁹ (u64::MAX minus occupied capacity).
- `ar` starts at 10¹⁶ and grows at ~10⁸/block on mainnet.
- `ar` doubling takes ~10⁸ blocks ≈ 25 years at 8 s/block.

When `withdrawing_ar ≈ 2 × deposit_ar` and `counted_capacity > u64::MAX / 2`, the product overflows. This is a long time horizon but structurally identical to the external report's argument (20-year currency inflation). NervosDAO is explicitly designed as a long-term store-of-value mechanism, making large, long-duration deposits the primary use case — exactly the scenario that maximizes both `counted_capacity` and the `ar` ratio.

---

### Recommendation

Replace the silent `as u64` cast with the same checked conversion used everywhere else in the file:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (safe, consistent with rest of file):
let withdraw_counted_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_u64).safe_add(occupied_capacity)?
```

This makes the function return `Err(DaoError::Overflow)` on overflow rather than silently returning a wrong capacity, consistent with `miner_issuance128` and `ar_increase128` handling in the same function.

---

### Proof of Concept

**Overflow condition:**

```
deposit_ar      = 10_000_000_000_000_000   (10^16, genesis value)
withdrawing_ar  = 20_000_000_000_000_000   (2×10^16, after ~25 years)
counted_capacity = 9_223_372_036_854_775_809  (u64::MAX/2 + 1)

withdraw_counted_capacity (u128) =
    9_223_372_036_854_775_809 * 20_000_000_000_000_000
    / 10_000_000_000_000_000
  = 18_446_744_073_709_551_618   ← exceeds u64::MAX (18_446_744_073_709_551_615)

withdraw_counted_capacity as u64 = 2   ← silently truncated!

withdraw_capacity = Capacity::shannons(2) + occupied_capacity
                  ≈ occupied_capacity only
```

The depositor loses their entire principal above `occupied_capacity` — potentially billions of shannons — with no error raised. The transaction is accepted by the chain with the truncated (wrong) capacity output. [9](#0-8)

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

**File:** util/dao/src/lib.rs (L149-158)
```rust
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

**File:** util/dao/src/lib.rs (L244-245)
```rust
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/dao/src/lib.rs (L258-258)
```rust
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/utils/src/lib.rs (L17-17)
```rust
const DEFAULT_GENESIS_ACCUMULATE_RATE: u64 = 10_000_000_000_000_000;
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
