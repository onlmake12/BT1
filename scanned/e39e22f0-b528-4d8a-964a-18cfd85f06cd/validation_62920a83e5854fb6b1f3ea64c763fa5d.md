### Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Produces Wrong DAO Withdrawal Capacity — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the maximum capacity a depositor can withdraw from the NervosDAO using the formula `counted_capacity * withdrawing_ar / deposit_ar`. The intermediate result is held in a `u128`, but is then cast to `u64` with a bare `as u64` (a silently truncating cast). Every other analogous `u128 → u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, which propagates an error on overflow. The inconsistency means that when the product exceeds `u64::MAX`, the function silently returns a **wrong (too-small) capacity** instead of an error, causing downstream fee verification to reject a legitimate DAO withdrawal transaction and permanently locking the deposited funds.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
// lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast silently wraps: if `withdraw_counted_capacity > u64::MAX`, the low 64 bits are kept and the high bits are discarded. The returned `withdraw_capacity` is then far smaller than the true maximum.

Every other `u128 → u64` narrowing in the same file is done safely:

```rust
// line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
// line 245
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)
// line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The overflow condition is `counted_capacity * withdrawing_ar > u64::MAX * deposit_ar`, i.e., the AR ratio `withdrawing_ar / deposit_ar` has grown enough relative to the cell's counted capacity. The existing test `check_withdraw_calculation_overflows` only catches the sub-case where the truncated value plus `occupied_capacity` still overflows `u64` (triggering `safe_add`'s error). It does **not** catch the silent-success case where the truncated value is small enough that `safe_add` succeeds, returning a silently wrong result. [5](#0-4) 

`calculate_maximum_withdraw` is called from `transaction_maximum_withdraw`, which feeds `transaction_fee`:

```rust
pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
    let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
    rtx.transaction
        .outputs_capacity()
        .and_then(|y| maximum_withdraw.safe_sub(y))  // fails if max is too small
        .map_err(Into::into)
}
``` [6](#0-5) 

`transaction_fee` is invoked by `TransactionVerifier` during block and tx-pool verification: [7](#0-6) 

When the truncated maximum is smaller than the transaction's actual output capacity, `safe_sub` returns `CapacityError::Overflow`, causing the withdrawal transaction to be rejected as invalid. The deposited funds become permanently unwithdrawable.

The same function is also exposed via the `calculate_dao_maximum_withdraw` RPC, which would silently return a wrong (too-small) value to callers constructing withdrawal transactions, causing them to build transactions that are then rejected. [8](#0-7) 

---

### Impact Explanation

A DAO depositor with a sufficiently large cell (counted capacity) who waits long enough for the accumulate rate (`ar`) to grow will find their withdrawal transaction permanently rejected by every node. The deposited CKB is locked in the DAO cell with no valid withdrawal path. The error is silent — the node returns a capacity value rather than an overflow error — so wallet software and the RPC caller receive no indication that the computed maximum is wrong.

---

### Likelihood Explanation

The overflow condition requires `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. The total CKB supply is approximately 3.36 × 10¹⁸ shannons, so a single cell can hold at most that amount. For overflow, the AR ratio must exceed `u64::MAX / 3.36e18 ≈ 5.5×`. The AR starts at `10_000_000_000` and grows slowly via secondary issuance; reaching a 5.5× multiple would take an extremely long time on mainnet. Likelihood is therefore low in the near term, but the defect is a real code inconsistency that will eventually become reachable as the chain ages, and it is already reachable on any chain (devnet, testnet) where the genesis AR or secondary issuance parameters are set to non-standard values.

---

### Recommendation

Replace the silent cast with the same checked pattern used everywhere else in the file:

```rust
// Before (line 156):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After:
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?
``` [9](#0-8) 

---

### Proof of Concept

Using the existing test harness pattern from `check_withdraw_calculation_overflows`:

```
deposit_ar  = 10_000_000_000_000_000   (10^16, hypothetical high starting AR)
withdrawing_ar = 55_000_000_000_000_000  (5.5× deposit_ar)
counted_capacity = 3_360_000_000_000_000_000  (3.36 × 10^18 shannons, ~total supply)

withdraw_counted_capacity (u128) = 3_360_000_000_000_000_000 * 55_000_000_000_000_000
                                   / 10_000_000_000_000_000
                                 = 18_480_000_000_000_000_000  (> u64::MAX ≈ 1.844 × 10^19)

as u64 truncation → 18_480_000_000_000_000_000 mod 2^64
                  = 18_480_000_000_000_000_000 - 18_446_744_073_709_551_616
                  = 33_255_926_290_448_384   (≈ 3.3 × 10^16, far below true value)

safe_add(occupied_capacity) succeeds → returns ~3.3 × 10^16 shannons
True maximum should be ~1.848 × 10^19 shannons

transaction_fee: maximum_withdraw.safe_sub(outputs_capacity)
  → outputs_capacity (correct) >> truncated maximum → DaoError::Overflow
  → withdrawal transaction permanently rejected
``` [10](#0-9) [11](#0-10)

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

**File:** util/dao/src/lib.rs (L204-204)
```rust
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
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

**File:** util/dao/src/tests.rs (L296-350)
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
}
```

**File:** verification/src/transaction_verifier.rs (L1-1)
```rust
use crate::cache::Completed;
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

**File:** util/dao/utils/src/error.rs (L36-41)
```rust
    /// Calculation overflow
    #[error("Overflow")]
    Overflow,
    /// ZeroC
    #[error("ZeroC")]
    ZeroC,
```
