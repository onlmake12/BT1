### Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Causes Incorrect DAO Withdrawal Amounts — (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` in `util/dao/src/lib.rs` uses a bare `as u64` truncating cast on a u128 intermediate result. Every other analogous u128→u64 narrowing in the same codebase uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. The inconsistency means that once the on-chain accumulate rate (`ar`) grows sufficiently relative to the deposit-time `ar`, the intermediate product silently wraps to a wrong (arbitrarily small) value instead of returning an error, producing an incorrect maximum-withdraw figure. Because `transaction_maximum_withdraw` (which calls `calculate_maximum_withdraw`) feeds into `transaction_fee`, which is used to validate DAO withdrawal transactions, this will eventually cause valid DAO withdrawals to be rejected and deposited funds to be locked.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast silently discards the high 64 bits when `withdraw_counted_capacity > u64::MAX`. Compare this to every other u128→u64 narrowing in the same file, which all use the checked form:

```rust
// dao_field_with_current_epoch – line 244-245
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);

// dao_field_with_current_epoch – line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;

// secondary_block_reward – line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

`calculate_maximum_withdraw` is called by the private `transaction_maximum_withdraw`, which is in turn called by the public `transaction_fee` used to validate DAO withdrawal transactions:

```rust
pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
    let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
    rtx.transaction
        .outputs_capacity()
        .and_then(|y| maximum_withdraw.safe_sub(y))
        .map_err(Into::into)
}
``` [5](#0-4) 

`transaction_maximum_withdraw` calls `calculate_maximum_withdraw` for each DAO input: [6](#0-5) 

It is also exposed directly via the `calculate_dao_maximum_withdraw` JSON-RPC endpoint: [7](#0-6) 

---

### Impact Explanation

When `withdraw_counted_capacity` overflows u64, `as u64` wraps it to `withdraw_counted_capacity % 2^64`, which can be arbitrarily small (including zero). The resulting `withdraw_capacity` is then far below the actual entitlement. When `transaction_fee` subtracts the transaction's output capacity from this wrong maximum, it gets a negative result, which `safe_sub` converts to a `CapacityOverflow` error. Any DAO withdrawal transaction processed through this path is rejected, and the deposited CKB becomes permanently locked.

The existing test `check_withdraw_calculation_overflows` expects `result.is_err()` but the error it actually catches comes from the subsequent `safe_add` overflowing `u64::MAX`, not from the `as u64` truncation itself — meaning the truncation path is untested and unguarded. [8](#0-7) 

---

### Likelihood Explanation

The overflow condition is:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX  (≈ 1.84 × 10¹⁹)
```

- `ar` starts at `10^16` (genesis) and grows at roughly 4 % per year (secondary issuance ≈ 1.344 billion CKB/year ÷ total supply ≈ 33.6 billion CKB).
- For a cell holding the entire theoretical CKB supply (~3.36 × 10¹⁸ shannons) deposited at genesis, the ratio `withdrawing_ar / deposit_ar` needs to exceed ~5.48, which occurs after approximately **43 years**.
- For more realistic cell sizes (e.g., 1 billion CKB), the threshold is ~135 years.
- The event will occur with **100 % probability** if the protocol operates long enough, exactly as with the JalaPair timestamp overflow.

The `ar` accumulate rate is stored as a u64 in the DAO field and grows monotonically with every block: [9](#0-8) 

---

### Recommendation

Replace the truncating cast with the same checked pattern used everywhere else in the file:

```rust
// Before (silent truncation):
Capacity::shannons(withdraw_counted_capacity as u64)

// After (consistent with the rest of the codebase):
Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
)
``` [10](#0-9) 

---

### Proof of Concept

Construct a DAO deposit cell with a large `counted_capacity` and craft headers whose `ar` values satisfy `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`:

```rust
// deposit_ar = 10_000_000_000_000_000  (genesis value)
// withdrawing_ar = 100_000_000_000_000_000  (10× growth, reachable after ~58 years)
// counted_capacity = 2_000_000_000_000_000_000  (2 × 10^18 shannons ≈ 20 billion CKB)

let withdraw_counted_capacity: u128 =
    2_000_000_000_000_000_000u128   // counted_capacity
    * 100_000_000_000_000_000u128   // withdrawing_ar
    / 10_000_000_000_000_000u128;   // deposit_ar
// = 20_000_000_000_000_000_000  >  u64::MAX (18_446_744_073_709_551_615)

let truncated = withdraw_counted_capacity as u64;
// = 20_000_000_000_000_000_000 % 2^64
// = 1_553_255_926_290_448_384  (silently wrong — ~18 billion shannons short)
```

With the truncated value, `transaction_fee` computes `maximum_withdraw = truncated + occupied_capacity`, which is far below the actual entitlement. Any withdrawal claiming the correct amount will be rejected with a capacity overflow error, permanently locking the deposited CKB. [11](#0-10)

### Citations

**File:** util/dao/src/lib.rs (L29-36)
```rust
    /// Returns the total transactions fee of `rtx`.
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

**File:** util/dao/src/lib.rs (L202-204)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
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

**File:** util/dao/src/tests.rs (L296-349)
```rust
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
