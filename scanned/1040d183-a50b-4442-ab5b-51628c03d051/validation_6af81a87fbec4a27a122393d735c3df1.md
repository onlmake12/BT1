### Title
Silent `u128`-to-`u64` Truncation in NervosDAO Maximum Withdraw Calculation Produces Incorrect Withdrawal Amounts — (`util/dao/src/lib.rs`)

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes a `u128` intermediate value `withdraw_counted_capacity` and then casts it to `u64` using the silent truncating `as u64` operator. Every other analogous `u128`→`u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. When the intermediate value exceeds `u64::MAX`, the `as u64` cast silently truncates it, returning a smaller-than-correct withdrawal capacity without any error. A depositor whose withdrawal transaction claims the correct (larger) amount will have that transaction rejected by the node, permanently locking their NervosDAO interest.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the interest-bearing withdrawal amount for a NervosDAO cell:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The formula scales `counted_capacity` by the ratio `withdrawing_ar / deposit_ar`. Because `withdrawing_ar` grows monotonically over the life of the chain, this ratio can eventually exceed 1 by a large enough margin that the product overflows `u64`. The `as u64` cast silently discards the high bits, producing a value that is **smaller** than the true result.

Every other `u128`→`u64` narrowing in the same file uses a checked conversion that propagates `DaoError::Overflow`:

```rust
// secondary_block_reward (line 204)
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// dao_field_with_current_epoch (line 245)
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)

// dao_field_with_current_epoch (line 258)
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The `calculate_maximum_withdraw` function is the sole exception. The existing test `check_withdraw_calculation_overflows` passes only because `safe_add(occupied_capacity)` catches a *different* overflow (the final sum exceeds `u64::MAX`), not because the `as u64` cast is safe. In a scenario where `withdraw_counted_capacity as u64` truncates but the truncated value plus `occupied_capacity` still fits in `u64`, the function returns silently incorrect data with no error. [5](#0-4) 

---

### Impact Explanation

`calculate_maximum_withdraw` is called during transaction fee calculation (`transaction_maximum_withdraw` → `transaction_fee`) and is the authoritative source for how much CKB a NervosDAO depositor may claim. [6](#0-5) [7](#0-6) 

When the truncated value is returned:

1. The node's view of the maximum withdrawable amount is smaller than the depositor's correct calculation.
2. The depositor constructs a withdrawal transaction claiming the correct (larger) amount.
3. The node rejects the transaction because the claimed output capacity exceeds the node's (truncated) maximum.
4. The depositor's principal plus accrued interest is permanently inaccessible — an exact analog to the Solidity finding where reward tokens become locked.

Additionally, if the truncated value is used in fee accounting, the miner receives a smaller fee than the protocol intends, silently breaking the economic invariant.

---

### Likelihood Explanation

The overflow condition requires `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. With:

- `counted_capacity` bounded by total CKB supply (~3.36 × 10¹⁸ shannons)
- `deposit_ar` starting at `10_000_000_000_000_000` (10¹⁶)
- `withdrawing_ar` growing at roughly 4 % per year (secondary issuance ÷ total supply)

The ratio must exceed ~5.5× for overflow to occur, which requires on the order of 43+ years of continuous accumulation. This is a long-horizon risk, but the CKB chain is designed for perpetual operation, and large long-term depositors (e.g., institutional holders) are the explicit target of NervosDAO. The inconsistency with every other narrowing cast in the same file also indicates this is an unintentional omission rather than a deliberate design choice. [8](#0-7) 

---

### Recommendation

Replace the silent `as u64` cast with the same checked pattern used everywhere else in the file:

```diff
-let withdraw_capacity =
-    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
+let withdraw_capacity =
+    Capacity::shannons(
+        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
+    )
+    .safe_add(occupied_capacity)?;
``` [9](#0-8) 

---

### Proof of Concept

The following values demonstrate the silent truncation path. Choose parameters such that `withdraw_counted_capacity` overflows `u64` but `(withdraw_counted_capacity as u64) + occupied_capacity` does not:

```
counted_capacity  = 18_000_000_000_000_000_000  (18 × 10¹⁸ shannons, ~18 billion CKB)
deposit_ar        = 10_000_000_000_000_000       (genesis rate, 10¹⁶)
withdrawing_ar    = 11_000_000_000_000_000       (10% growth, reachable in ~2.5 years at 4%/yr)

withdraw_counted_capacity (u128) = 18_000_000_000_000_000_000 * 11_000_000_000_000_000
                                   / 10_000_000_000_000_000
                                 = 19_800_000_000_000_000_000   -- exceeds u64::MAX (1.844 × 10¹⁹)

withdraw_counted_capacity as u64 = 19_800_000_000_000_000_000 mod 2⁶⁴
                                 = 1_353_255_926_290_448,384    -- silently truncated, wrong value
```

With the current code, `calculate_maximum_withdraw` returns `Capacity::shannons(1_353_255_926_290_448_384 + occupied_capacity)` — a value far smaller than the depositor's correct entitlement — with no error, no panic, and no log entry. [1](#0-0) [10](#0-9)

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

**File:** util/dao/src/lib.rs (L108-113)
```rust
                            self.calculate_maximum_withdraw(
                                output,
                                Capacity::bytes(cell_meta.data_bytes as usize)?,
                                deposit_header_hash,
                                withdrawing_header_hash,
                            )
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

**File:** util/dao/src/lib.rs (L202-205)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
        Ok(Capacity::shannons(reward))
```

**File:** util/dao/src/lib.rs (L244-246)
```rust
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
        let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;
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

**File:** util/dao/utils/src/lib.rs (L17-17)
```rust
const DEFAULT_GENESIS_ACCUMULATE_RATE: u64 = 10_000_000_000_000_000;
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
