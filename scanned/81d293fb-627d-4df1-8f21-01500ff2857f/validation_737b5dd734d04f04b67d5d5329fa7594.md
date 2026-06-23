### Title
Unsafe Truncating Cast in `calculate_maximum_withdraw` Silently Corrupts DAO Withdrawal Capacity - (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` in `util/dao/src/lib.rs` computes `withdraw_counted_capacity` as a `u128` intermediate value, then casts it to `u64` using the Rust `as u64` operator — a silent truncating cast that discards the upper 64 bits without any overflow check or error. This is the direct analog of the Sushi `_getAmountsForLiquidity` bug: an explicit narrowing cast on a critical financial quantity with no guard against overflow.

---

### Finding Description

At line 156 of `util/dao/src/lib.rs`, the DAO withdrawal capacity is computed as:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The multiplication `counted_capacity * withdrawing_ar` is correctly widened to `u128` to avoid overflow. However, after dividing by `deposit_ar`, the result is cast back to `u64` with `as u64` — a Rust truncating cast that silently discards the high 64 bits if the value exceeds `u64::MAX`. No error is returned; the silently wrong value is passed directly into `Capacity::shannons(...)`.

Compare this to the analogous safe patterns used elsewhere in the same codebase, where `u64::try_from(...).map_err(|_| DaoError::Overflow)?` is used:

```rust
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
``` [2](#0-1) 

```rust
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [3](#0-2) 

The `calculate_maximum_withdraw` function is the only place in the DAO calculator that uses the unsafe `as u64` cast instead of `try_from`.

---

### Impact Explanation

When `withdraw_counted_capacity` exceeds `u64::MAX`, the `as u64` cast wraps the value modulo 2^64, producing a drastically smaller (and incorrect) result. Two concrete consequences follow:

1. **Wrong RPC estimate**: The `calculate_dao_maximum_withdraw` RPC endpoint calls `calculate_maximum_withdraw` directly and returns the truncated value to callers, causing wallets and tooling to compute an incorrect (far too small) maximum withdrawal amount. [4](#0-3) 

2. **Tx-pool DoS for valid DAO withdrawals**: `transaction_fee` calls `transaction_maximum_withdraw` → `calculate_maximum_withdraw`. If the truncated `maximum_withdraw` is smaller than `outputs_capacity`, `safe_sub` returns `Err`, causing the tx-pool to reject a valid DAO withdrawal transaction. A depositor with a sufficiently large cell cannot withdraw their funds through the node. [5](#0-4) 

The existing test `check_withdraw_calculation_overflows` asserts `result.is_err()` for a near-maximal capacity cell, but the overflow it catches is in the subsequent `safe_add(occupied_capacity)` call — not in the `as u64` cast itself. A carefully chosen capacity value where `(withdraw_counted_capacity as u64) + occupied_capacity` does not overflow `u64` would produce `Ok(wrong_small_value)` instead of `Err`, silently returning a corrupted withdrawal amount with no error signal. [6](#0-5) 

---

### Likelihood Explanation

The overflow condition requires `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. Since `counted_capacity ≤ u64::MAX` and `withdrawing_ar/deposit_ar` is the DAO accumulation rate growth ratio (which starts at 1 and grows slowly), triggering the overflow requires a DAO cell holding capacity close to `u64::MAX` shannons (≈ 184 billion CKB). CKB's total issuance is bounded well below this, making the condition practically unreachable on mainnet today. Likelihood is therefore **low** in practice, though the code defect is real and the correct fix is straightforward.

---

### Recommendation

Replace the unsafe truncating cast with a checked conversion, consistent with the pattern already used in `dao_field_with_current_epoch` and `secondary_block_reward`:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (safe):
let withdraw_counted_u64 = u64::try_from(withdraw_counted_capacity)
    .map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_u64).safe_add(occupied_capacity)?
``` [7](#0-6) 

---

### Proof of Concept

Construct a DAO cell and headers such that `withdraw_counted_capacity` overflows `u64` but the truncated value plus `occupied_capacity` does not:

```
counted_capacity  = u64::MAX - occupied_capacity  (e.g., 18_446_744_069_609_551_615)
deposit_ar        = 10_000_000_000_000_000
withdrawing_ar    = 10_000_000_002_000_000

withdraw_counted_capacity (u128) =
    18_446_744_069_609_551_615 * 10_000_000_002_000_000 / 10_000_000_000_000_000
  ≈ 18_446_744_069_609_551_615 + 3_689_348_813
  = 18_446_744_073_298_900_428   # still < u64::MAX, no overflow here

# To trigger the bug, use a larger ratio:
withdrawing_ar = 10_000_000_010_000_000

withdraw_counted_capacity (u128) ≈
    18_446_744_069_609_551_615 + 18_446_744_069  # ≈ 18_446_744_088_056_295_684
  > u64::MAX (18_446_744_073_709_551_615)

# as u64 truncates to:
18_446_744_088_056_295_684 - 2^64 = 14_346_744_068

# safe_add(occupied_capacity) succeeds with ~143 CKB
# instead of the correct ~184 billion CKB
# calculate_maximum_withdraw returns Ok(~143 CKB) — silently wrong, no error
```

The depositor's withdrawal is silently computed as ~143 CKB instead of the correct value, and the tx-pool or RPC caller receives no indication that anything went wrong.

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

**File:** util/dao/src/lib.rs (L244-245)
```rust
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/dao/src/lib.rs (L258-258)
```rust
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

**File:** rpc/src/module/experiment.rs (L259-266)
```rust
                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash.into(),
                ) {
                    Ok(capacity) => Ok(capacity.into()),
                    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
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
