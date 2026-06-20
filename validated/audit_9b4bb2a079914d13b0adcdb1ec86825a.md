### Title
Silent `u128`-to-`u64` Truncation in `DaoCalculator::calculate_maximum_withdraw` Returns Wrong Withdrawal Capacity — (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the DAO withdrawal amount using a `u128` intermediate value but converts it to `u64` with a silent truncating `as u64` cast. Every other analogous calculation in the same file uses the checked `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern. When the intermediate `u128` result exceeds `u64::MAX`, the cast silently wraps, producing a drastically wrong (too-small) withdrawal capacity. This is the direct CKB analog of the external report's boundary-condition arithmetic error: a ratio-scaled amount calculation returns an incorrect value at a specific boundary, corrupting capacity accounting.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the scaled withdrawal amount as:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast at line 156 is a **silent truncating cast**. If `withdraw_counted_capacity > u64::MAX`, the high bits are silently discarded and the result is a completely wrong (too-small) capacity value. No error is returned.

Compare this to every other `u128`-to-`u64` conversion in the same file, all of which use the checked pattern:

```rust
// secondary_block_reward (line 204)
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// dao_field_with_current_epoch (line 244-245)
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);

// dao_field_with_current_epoch (line 258)
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The existing overflow test `check_withdraw_calculation_overflows` only catches the `safe_add` overflow at the end of the function (when `withdraw_counted_capacity as u64 + occupied_capacity > u64::MAX`). It does **not** catch the silent truncation case where `withdraw_counted_capacity > u64::MAX` but `(withdraw_counted_capacity as u64) + occupied_capacity <= u64::MAX` — in that scenario the function returns `Ok(wrong_value)` with no error. [5](#0-4) 

The overflow condition requires:

```
counted_capacity × withdrawing_ar / deposit_ar > u64::MAX
```

Since `ar` starts at `10^16` and grows monotonically by `ar × g2 / C` per block, and `counted_capacity` can be up to ~`u64::MAX` shannons, the product `counted_capacity × withdrawing_ar` can exceed `u64::MAX × deposit_ar` after sufficient chain operation. The `secondary_block_issuance` function that feeds `ar` growth has no cap on cumulative `ar`: [6](#0-5) 

---

### Impact Explanation

When the truncation occurs:

1. **`calculate_dao_maximum_withdraw` RPC** returns a silently wrong (too-small) withdrawal amount to the caller. A user relying on this to construct a withdrawal transaction will build a transaction with an output capacity that is too small, losing funds.
2. **`transaction_maximum_withdraw`** (called from `transaction_fee` during tx-pool admission and block verification) computes a wrong maximum withdraw, which then feeds into `maximum_withdraw.safe_sub(outputs_capacity)`. If the truncated value is smaller than the actual output capacity, the transaction is rejected with a spurious error, permanently locking DAO funds.
3. The DAO `S` (secondary issuance accumulator) field in block headers is also computed using `withdrawed_interests`, which calls `transaction_maximum_withdraw`. A wrong value here corrupts the on-chain DAO accounting state. [7](#0-6) [8](#0-7) 

---

### Likelihood Explanation

The `ar` accumulate rate grows slowly (fractions of a percent per year under normal CKB economics). For `counted_capacity × withdrawing_ar / deposit_ar` to exceed `u64::MAX`, either:
- A very large deposit (close to `u64::MAX` shannons) is held for many decades, or
- The `ar` ratio grows unusually fast (e.g., if `C` is small relative to `g2`).

This makes the likelihood **low** under current mainnet conditions. However, the code inconsistency is unambiguous — every other `u128`-to-`u64` conversion in the same file uses the safe checked pattern, making this a clear defect that will become exploitable as the chain matures. Any RPC caller or transaction submitter can trigger the code path without any privilege. [9](#0-8) 

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
```

Add a test case where `counted_capacity × withdrawing_ar / deposit_ar` exceeds `u64::MAX` but `(result as u64) + occupied_capacity` does not, verifying that `Err(DaoError::Overflow)` is returned rather than a silently wrong `Ok(...)`.

---

### Proof of Concept

**Setup:**
- `deposit_ar = 10_000_000_000_000_000` (initial `ar`)
- `withdrawing_ar = 20_000_000_000_000_000` (ar doubled — achievable after many years)
- `counted_capacity = 10_000_000_000_000_000_000` shannons (~10^19, within u64 range)
- `occupied_capacity = 4_100_000_000` shannons (41 bytes)

**Calculation:**
```
withdraw_counted_capacity (u128)
  = 10_000_000_000_000_000_000 × 20_000_000_000_000_000
    / 10_000_000_000_000_000
  = 20_000_000_000_000_000_000   ← exceeds u64::MAX (18_446_744_073_709_551_615)
```

**With `as u64` (current code):**
```
20_000_000_000_000_000_000 as u64
  = 20_000_000_000_000_000_000 mod 2^64
  = 1_553_255_926_290_448_384   ← silently wrong
withdraw_capacity = 1_553_255_926_290_448_384 + 4_100_000_000
                  = 1_553_255_930_390_448_384   ← Ok(wrong value), no error
```

**With `u64::try_from` (correct code):**
```
u64::try_from(20_000_000_000_000_000_000) → Err(TryFromIntError)
→ DaoError::Overflow   ← correct behavior
```

The function currently returns a silently wrong capacity instead of an error, causing the caller to receive an incorrect withdrawal amount with no indication of failure. [10](#0-9)

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

**File:** util/dao/src/lib.rs (L127-159)
```rust
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

**File:** util/dao/src/lib.rs (L242-246)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
        let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;
```

**File:** util/dao/src/lib.rs (L252-254)
```rust
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;
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

**File:** util/types/src/core/extras.rs (L255-266)
```rust
    pub fn secondary_block_issuance(
        &self,
        block_number: BlockNumber,
        secondary_epoch_issuance: Capacity,
    ) -> CapacityResult<Capacity> {
        let mut g2 = Capacity::shannons(secondary_epoch_issuance.as_u64() / self.length());
        let remainder = secondary_epoch_issuance.as_u64() % self.length();
        if block_number >= self.start_number() && block_number < self.start_number() + remainder {
            g2 = g2.safe_add(Capacity::one())?;
        }
        Ok(g2)
    }
```
