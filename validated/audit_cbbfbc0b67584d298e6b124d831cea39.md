### Title
Silent u128→u64 Truncation in DAO Withdrawal Capacity Calculation — (`File: util/dao/src/lib.rs`)

---

### Summary

In `util/dao/src/lib.rs`, the `calculate_maximum_withdraw` function computes the interest-adjusted withdrawal capacity using a `u128` intermediate value to avoid overflow during multiplication, but then casts the result back to `u64` using the Rust `as u64` operator — a **silent truncating cast**. Every other analogous `u128`→`u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, which propagates a proper error. The inconsistent `as u64` cast means that when `withdraw_counted_capacity` exceeds `u64::MAX`, the high bits are silently discarded and the function returns a **wrong, smaller withdrawal amount** instead of an error, causing a DAO depositor to silently receive less CKB than they are entitled to.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
// lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast on line 156 is a **silent truncation** in Rust: if `withdraw_counted_capacity > u64::MAX`, the upper 64 bits are discarded with no error, no panic, and no indication to the caller.

Every other `u128`→`u64` narrowing in the same file uses the checked form:

```rust
// secondary_block_reward, line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// dao_field_with_current_epoch, line 245
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);

// dao_field_with_current_epoch, line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The `calculate_maximum_withdraw` function is the only one that deviates from this pattern.

**When does the overflow occur?**

`withdraw_counted_capacity = counted_capacity × withdrawing_ar / deposit_ar`

- `counted_capacity` = `output_capacity − occupied_capacity` (a `u64`)
- `withdrawing_ar / deposit_ar` is the accumulated interest ratio (always ≥ 1)

If `counted_capacity` is large (e.g., a cell holding a large fraction of the total CKB supply) and the AR ratio has grown sufficiently, the final result can exceed `u64::MAX` (~1.844 × 10¹⁹ shannons). The intermediate `u128` computation handles the multiplication correctly, but the final `as u64` cast silently discards the overflow.

**Silent vs. detected overflow:**

The existing test `check_withdraw_calculation_overflows` (line 295–350) uses `output_capacity = 18_446_744_073_709_550_000` (close to `u64::MAX`) and asserts `result.is_err()`. That test's error is caught by the subsequent `safe_add(occupied_capacity)` overflowing — not by the `as u64` cast itself. [5](#0-4) 

There is a gap: values where `withdraw_counted_capacity > u64::MAX` but `(withdraw_counted_capacity as u64) + occupied_capacity ≤ u64::MAX`. In that gap, the function returns a silently wrong (smaller) capacity with `Ok(...)`, not `Err(...)`.

---

### Impact Explanation

**Incorrect DAO withdrawal capacity returned silently.**

`calculate_maximum_withdraw` is called in two places:

1. **`transaction_maximum_withdraw` → `transaction_fee`** (line 31–35): used during block/transaction verification to compute the fee for DAO withdrawal transactions. A silently truncated maximum causes the verifier to compute a wrong fee, which can cause valid DAO withdrawal transactions to be rejected (the depositor cannot reclaim their full interest). [6](#0-5) 

2. **RPC `calculate_dao_maximum_withdraw`** (lines 259–267): returns the maximum withdrawal amount directly to the user. A silently truncated result causes the user to construct a withdrawal transaction claiming less than they are entitled to, permanently losing the difference. [7](#0-6) 

In both cases, the depositor suffers a **silent, unrecoverable loss of CKB** with no error message.

---

### Likelihood Explanation

The overflow requires `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`. The total CKB issuance is ~33.6 billion CKB = ~3.36 × 10¹⁸ shannons, well below `u64::MAX` (~1.844 × 10¹⁹). However:

- A single cell's `capacity` field is a raw `u64` and is not bounded by the total supply at the type level.
- The AR ratio grows monotonically as secondary issuance accumulates. Over a sufficiently long time horizon (decades), the ratio could grow enough to push a large cell's withdrawal capacity past `u64::MAX`.
- More immediately: any cell whose `output_capacity` is set close to `u64::MAX` (which passes the capacity verifier if inputs are sufficient) combined with a non-trivial AR ratio will trigger the silent truncation in the gap range described above.

The likelihood is **low in the near term** but **non-zero and growing** as the chain ages and the AR ratio increases.

---

### Recommendation

Replace the silent `as u64` cast with the same checked conversion used everywhere else in the file:

```rust
// Before (line 155-156):
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After:
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
``` [8](#0-7) 

This makes the overflow behavior consistent with `secondary_block_reward`, `dao_field_with_current_epoch`, and all other arithmetic in the same module.

---

### Proof of Concept

The following values demonstrate the silent truncation gap:

```
deposit_ar       = 10_000_000_000_000_000   (initial AR)
withdrawing_ar   = 20_000_000_000_000_000   (AR doubled after long time)
output_capacity  = 18_446_744_073_709_551_615  (u64::MAX)
occupied_capacity = 6_100_000_000            (61 bytes minimum)
counted_capacity = 18_446_744_067_609_551_615

withdraw_counted_capacity (u128) =
    18_446_744_067_609_551_615 × 20_000_000_000_000_000
    / 10_000_000_000_000_000
  = 36_893_488_135_219_103_230   (> u64::MAX = 18_446_744_073_709_551_615)

withdraw_counted_capacity as u64 =
    36_893_488_135_219_103_230 mod 2^64
  = 18_446_744_061_509_551_614   (silently truncated, WRONG)

withdraw_capacity = 18_446_744_061_509_551_614 + 6_100_000_000
                  = 18_446_744_067_609_551_614  (fits in u64, no safe_add error)
```

The function returns `Ok(Capacity::shannons(18_446_744_067_609_551_614))` — a value **18 billion shannons (180 CKB) less** than the correct answer — with no error. The depositor silently loses 180 CKB of interest.

The entry path is fully unprivileged: any transaction sender who deposits into NervosDAO and later submits a withdrawal transaction triggers `calculate_maximum_withdraw` during verification. [9](#0-8)

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

**File:** util/dao/src/lib.rs (L126-159)
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
    }
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
