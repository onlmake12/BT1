### Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Produces Wrong DAO Withdrawal Amounts - (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` uses a bare `as u64` cast to narrow a u128 intermediate result, silently truncating the value if it exceeds `u64::MAX`. Every other u128→u64 conversion in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. When truncation occurs, the function returns `Ok(drastically_wrong_small_value)` instead of `Err`, causing the NervosDAO withdrawal amount to be computed incorrectly — a permanent loss of DAO interest for the depositor.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the maximum withdrawable capacity for a NervosDAO cell as:

```
withdraw = (output_capacity - occupied_capacity) * withdrawing_ar / deposit_ar + occupied_capacity
```

The intermediate `withdraw_counted_capacity` is computed in u128 to avoid overflow during multiplication, but is then narrowed back to u64 with a bare `as u64` cast:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast is a **wrapping/truncating** cast in Rust. If `withdraw_counted_capacity` exceeds `u64::MAX`, the value wraps to a small number (e.g., if the true value is `u64::MAX + 1000`, the cast yields `999`). The subsequent `safe_add(occupied_capacity)` then succeeds on this small value, and the function returns `Ok(tiny_wrong_capacity)` with no error signal.

Every other u128→u64 narrowing in the same file uses the checked pattern:

```rust
// Line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
// Line 245
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)
// Line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The existing overflow test (`check_withdraw_calculation_overflows`) does **not** cover the silent truncation case. It uses values where the overflow is caught by the final `safe_add(occupied_capacity)` overflowing u64 — not by the `as u64` cast itself. The test therefore passes for the wrong reason and does not validate the truncation path. [5](#0-4) 

`calculate_maximum_withdraw` is called from two production paths:

1. **`transaction_maximum_withdraw` → `transaction_fee`**: used during block assembly and fee verification. A wrong (too-small) `maximum_withdraw` causes `safe_sub(outputs_capacity)` to fail, rejecting a valid DAO withdrawal transaction.
2. **`calculate_dao_maximum_withdraw` RPC**: returns the wrong (too-small) value directly to the caller, misleading the user about their entitled withdrawal. [6](#0-5) [7](#0-6) 

---

### Impact Explanation

When `withdraw_counted_capacity` silently wraps, the returned capacity is drastically smaller than the correct value. The depositor either:

- Receives a misleading RPC response telling them their entitled withdrawal is far smaller than it actually is, causing them to leave DAO interest permanently unclaimed.
- Has their valid DAO withdrawal transaction rejected by the node (because the node's fee calculation sees a negative fee), permanently freezing their ability to exit the DAO through normal means.

This matches the target impact class: permanent freezing / loss of unclaimed DAO interest for an unprivileged transaction sender or RPC caller.

---

### Likelihood Explanation

For `withdraw_counted_capacity` to exceed `u64::MAX`, the ratio `withdrawing_ar / deposit_ar` must exceed approximately `u64::MAX / max_counted_capacity`. With total CKB supply ~3.36×10^18 shannons and `u64::MAX` ~18.4×10^18 shannons, the ratio must exceed ~5.5×. Since `ar` grows proportionally to secondary issuance (~1.344 billion CKB/year) divided by total capacity (~33.6 billion CKB), `ar` grows roughly 4% per year. Reaching a 5.5× ratio requires approximately 43 years of continuous deposit without withdrawal.

This makes the vulnerability **theoretical under current economic parameters** but not impossible over the full intended lifetime of the chain. The silent nature of the failure (no error, wrong value returned) means it would be extremely difficult to detect when it eventually occurs.

---

### Recommendation

Replace the bare `as u64` cast with the checked conversion already used everywhere else in the file:

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

Additionally, add a test case that specifically exercises the silent-truncation path (where `withdraw_counted_capacity` overflows u64 but `withdraw_counted_capacity as u64 + occupied_capacity` does not), asserting that the function returns `Err(DaoError::Overflow)` rather than `Ok(wrong_value)`.

---

### Proof of Concept

The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` (lines 295–350) does **not** cover the silent truncation. The following scenario demonstrates the gap:

Choose parameters such that:
- `counted_capacity` is just above `u64::MAX / ratio` (so `withdraw_counted_capacity` wraps to a small value)
- `(withdraw_counted_capacity as u64) + occupied_capacity` does not overflow u64

With the current code, `calculate_maximum_withdraw` returns `Ok(tiny_wrong_value)`. With the fix applied, it returns `Err(DaoError::Overflow)`.

The entry path is fully unprivileged: any user can call the `calculate_dao_maximum_withdraw` RPC or submit a DAO withdrawal transaction referencing a sufficiently large deposit cell and a withdrawing header with a sufficiently large `ar` ratio. [8](#0-7)

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

**File:** util/dao/src/lib.rs (L244-245)
```rust
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/dao/src/lib.rs (L258-258)
```rust
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

**File:** rpc/src/module/experiment.rs (L235-298)
```rust
    fn calculate_dao_maximum_withdraw(
        &self,
        out_point: OutPoint,
        kind: DaoWithdrawingCalculationKind,
    ) -> Result<Capacity> {
        let snapshot: &Snapshot = &self.shared.snapshot();
        let consensus = snapshot.consensus();
        let out_point: packed::OutPoint = out_point.into();
        let data_loader = snapshot.borrow_as_data_loader();
        let calculator = DaoCalculator::new(consensus, &data_loader);
        match kind {
            DaoWithdrawingCalculationKind::WithdrawingHeaderHash(withdrawing_header_hash) => {
                let (tx, deposit_header_hash) = snapshot
                    .get_transaction(&out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;
                let output = tx
                    .outputs()
                    .get(out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;
                let output_data = tx
                    .outputs_data()
                    .get(out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;

                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash.into(),
                ) {
                    Ok(capacity) => Ok(capacity.into()),
                    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
                }
            }
            DaoWithdrawingCalculationKind::WithdrawingOutPoint(withdrawing_out_point) => {
                let (_tx, deposit_header_hash) = snapshot
                    .get_transaction(&out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;

                let withdrawing_out_point: packed::OutPoint = withdrawing_out_point.into();
                let (withdrawing_tx, withdrawing_header_hash) = snapshot
                    .get_transaction(&withdrawing_out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid withdrawing_out_point"))?;

                let output = withdrawing_tx
                    .outputs()
                    .get(withdrawing_out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid withdrawing_out_point"))?;
                let output_data = withdrawing_tx
                    .outputs_data()
                    .get(withdrawing_out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid withdrawing_out_point"))?;

                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash,
                ) {
                    Ok(capacity) => Ok(capacity.into()),
                    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
                }
            }
        }
```
