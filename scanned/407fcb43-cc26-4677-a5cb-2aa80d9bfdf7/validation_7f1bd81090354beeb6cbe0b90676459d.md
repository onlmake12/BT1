### Title
Silent `u128`-to-`u64` Truncation in `calculate_maximum_withdraw` Produces Incorrect DAO Withdrawal Amount - (File: `util/dao/src/lib.rs`)

---

### Summary

In `DaoCalculator::calculate_maximum_withdraw`, the intermediate result `withdraw_counted_capacity` (a `u128`) is narrowed to `u64` via a bare `as u64` cast. If the value exceeds `u64::MAX`, the upper bits are silently discarded, producing an incorrect — and too-small — withdrawal capacity. Every other analogous `u128 → u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, making this a clear, isolated inconsistency.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the DAO interest-adjusted withdrawal capacity:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The expression `withdraw_counted_capacity as u64` is a **truncating cast**: if `withdraw_counted_capacity > u64::MAX`, the high 64 bits are silently dropped and the function returns a drastically smaller capacity with no error.

Every other `u128 → u64` narrowing in the same `impl` block uses the checked form:

```rust
// line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// line 245
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)

// line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The `calculate_maximum_withdraw` function is the only site that uses the unsafe `as u64` cast.

---

### Impact Explanation

When `withdraw_counted_capacity` overflows `u64::MAX`, the cast silently wraps the value to a small number. The function then returns `Ok(...)` with a capacity far below the depositor's entitlement. The transaction is accepted on-chain with incorrect accounting: the depositor loses the interest they accrued, and the excess capacity is effectively burned or misattributed. No error is raised, so neither the node nor the user is alerted.

This function is called:
1. During transaction script verification (`transaction_maximum_withdraw` → `calculate_maximum_withdraw`) for every DAO withdrawal transaction submitted to the network.
2. Via the `calculate_dao_maximum_withdraw` RPC endpoint, which any unprivileged caller can invoke. [5](#0-4) [6](#0-5) 

---

### Likelihood Explanation

The overflow condition is:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

`counted_capacity` is bounded by the total CKB supply (~3.36 × 10¹⁸ shannons, roughly 18 % of `u64::MAX`). The accumulation rate (`ar`) starts at `10^16` and grows proportionally to secondary issuance (~4 % per year). For the ratio `withdrawing_ar / deposit_ar` to push the product above `u64::MAX` for a maximum-sized cell, the ratio would need to exceed ~5.5×, which at 4 % annual growth takes on the order of decades. The likelihood is therefore low on current mainnet timescales, but the code is provably incorrect and the inconsistency with every other narrowing in the same file is a clear defect that will eventually be reachable as the chain ages.

---

### Recommendation

Replace the silent cast with the same checked pattern used everywhere else in the file:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (consistent with the rest of the file):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?
``` [7](#0-6) 

---

### Proof of Concept

The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` already constructs a near-`u64::MAX` capacity cell and expects an error, but the error it currently observes may arise from a different path (e.g., `safe_sub` or `safe_add` on the final sum) rather than from the truncation itself. A targeted PoC:

```rust
// deposit_ar = 10_000_000_000_000_000  (10^16, genesis value)
// withdrawing_ar = 60_000_000_000_000_000  (6× growth — reachable after ~45 years)
// counted_capacity = u64::MAX / 5 + 1  = 3_689_348_814_741_910_324

let withdraw_counted_capacity: u128 =
    u128::from(3_689_348_814_741_910_324u64)   // counted_capacity
    * 60_000_000_000_000_000u128               // withdrawing_ar
    / 10_000_000_000_000_000u128;              // deposit_ar
// = 22_136_092_888_451_461_944  >  u64::MAX (18_446_744_073_709_551_615)

let truncated = withdraw_counted_capacity as u64;
// = 22_136_092_888_451_461_944 - 18_446_744_073_709_551_616
// = 3_689_348_814_741_910_328   ← wrong, ~6× too small
```

The function returns `Ok(Capacity::shannons(3_689_348_814_741_910_328))` instead of `Err(DaoError::Overflow)`, silently crediting the depositor with only ~1/6 of their entitled withdrawal. [8](#0-7) [9](#0-8)

### Citations

**File:** util/dao/src/lib.rs (L108-113)
```rust
                            self.calculate_maximum_withdraw(
                                output,
                                Capacity::bytes(cell_meta.data_bytes as usize)?,
                                deposit_header_hash,
                                withdrawing_header_hash,
                            )
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

**File:** util/dao/src/lib.rs (L244-245)
```rust
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/dao/src/lib.rs (L258-261)
```rust
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;
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
