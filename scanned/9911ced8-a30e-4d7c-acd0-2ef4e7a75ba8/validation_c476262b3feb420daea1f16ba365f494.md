### Title
Silent `u128→u64` Truncation in DAO Withdrawal Capacity Calculation Causes Under-Disbursement Without Error — (File: `util/dao/src/lib.rs`)

### Summary
`DaoCalculator::calculate_maximum_withdraw` computes the entitled withdrawal capacity using a `u128` intermediate value but narrows it to `u64` with a bare `as u64` cast. This is a **silent truncating cast**: if the intermediate result exceeds `u64::MAX`, the lower 64 bits are silently kept, the subsequent `safe_add` may succeed without error, and the function returns a capacity that is far less than the depositor is entitled to. Every other analogous `u128→u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, making this a clear inconsistency with a concrete under-disbursement impact.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the withdrawable capacity as:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast on a `u128` value is defined in Rust to **silently keep only the lower 64 bits** (no panic, no error). If `withdraw_counted_capacity > u64::MAX`, the result is `withdraw_counted_capacity mod 2^64`, which can be arbitrarily small. If that truncated value plus `occupied_capacity` still fits in `u64`, `safe_add` succeeds and the function returns a silently wrong (under-counted) capacity.

**Contrast with every other `u128→u64` narrowing in the same file:**

| Location | Pattern used |
|---|---|
| `secondary_block_reward` (line 204) | `u64::try_from(reward128).map_err(\|_\| DaoError::Overflow)?` |
| `dao_field_with_current_epoch` (line 245) | `u64::try_from(miner_issuance128).map_err(\|_\| DaoError::Overflow)?` |
| `dao_field_with_current_epoch` (line 258) | `u64::try_from(ar_increase128).map_err(\|_\| DaoError::Overflow)?` |
| **`calculate_maximum_withdraw` (line 156)** | **`withdraw_counted_capacity as u64` ← silent truncation** | [2](#0-1) [3](#0-2) [4](#0-3) 

The existing overflow test `check_withdraw_calculation_overflows` uses a capacity of `18_446_744_073_709_550_000` shannons and asserts `result.is_err()`. However, that test only exercises the `safe_add` overflow path (the truncated value + `occupied_capacity` overflows `u64`). It does **not** cover the silent-truncation path where `withdraw_counted_capacity mod 2^64` is small enough that `safe_add` succeeds with a wrong result. [5](#0-4) 

---

### Impact Explanation

`calculate_maximum_withdraw` is called from three production paths:

1. **RPC `calculate_dao_maximum_withdraw`** — returns the wrong (smaller) entitlement to the user, causing them to construct a withdrawal transaction that claims less than they are owed. [6](#0-5) 

2. **`transaction_fee` / `transaction_maximum_withdraw`** — used by the tx-pool to compute the fee for a DAO withdrawal transaction. A silently truncated maximum-withdraw value causes the fee to be computed as `truncated_max_withdraw - outputs_capacity`, which could be a wildly wrong number (potentially causing the transaction to be rejected as having negative fee, or accepted with a miscalculated fee). [7](#0-6) 

3. **`withdrawed_interests` → `dao_field_with_current_epoch`** — the truncated withdrawal amount is subtracted from the DAO secondary-issuance accumulator `current_s`. A silently smaller `withdrawed_interests` causes `current_s` to be **over-counted**, corrupting the DAO field embedded in every subsequent block header. This propagates to all future AR and reward calculations. [8](#0-7) 

---

### Likelihood Explanation

The truncation condition is:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

where `counted_capacity = output_capacity − occupied_capacity`.

- `u64::MAX ≈ 1.84 × 10¹⁹` shannons.
- The total CKB issuance is ~33.6 billion CKB = `3.36 × 10¹⁸` shannons.
- For a cell holding the entire supply, the AR ratio must exceed `1.84e19 / 3.36e18 ≈ 5.5×` before truncation occurs.

Under normal network conditions this ratio is reached only after an extremely long time horizon, making exploitation **unlikely in the near term**. However:

- The bug is a **code inconsistency** — the identical pattern is handled correctly in three adjacent functions in the same file.
- The existing test suite does **not** cover the silent-success truncation path; it only covers the `safe_add` overflow path.
- The impact when triggered is **silent under-disbursement** (no error, no revert, wrong value returned), exactly mirroring the AFiBase report's vulnerability class.
- Any future change to cell capacity limits, genesis allocation, or AR growth parameters could bring the condition within reach.

---

### Recommendation

Replace the bare `as u64` cast with the same checked narrowing used everywhere else in the file:

```rust
// Before (silent truncation):
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After (consistent with secondary_block_reward and dao_field_with_current_epoch):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
```

Add a unit test that constructs a scenario where `withdraw_counted_capacity` falls in the range `(u64::MAX, 2×u64::MAX − occupied_capacity]` and asserts that the function returns `Err(DaoError::Overflow)` rather than a silently truncated `Ok(...)`.

---

### Proof of Concept

**Triggering the silent-success path** (pseudocode):

```
deposit_ar      = 10_000_000_000_000_000   (initial AR)
withdrawing_ar  = 20_000_000_000_000_000   (AR doubled — extreme but illustrative)
occupied_cap    = 4_100_000_000            (41-byte default CellOutput)
output_cap      = 18_446_744_073_709_551_615  (u64::MAX)

counted_capacity = u64::MAX - 4_100_000_000
                 = 18_446_744_069_609_551_615

withdraw_counted_capacity (u128)
  = 18_446_744_069_609_551_615 × 20_000_000_000_000_000
    / 10_000_000_000_000_000
  = 36_893_488_139_219_103_230   ← exceeds u64::MAX (18_446_744_073_709_551_615)

as u64 truncation:
  36_893_488_139_219_103_230 mod 2^64
  = 36_893_488_139_219_103_230 - 18_446_744_073_709_551_616
  = 18_446_744_065_509_551_614   ← silently small value

safe_add(4_100_000_000):
  18_446_744_065_509_551_614 + 4_100_000_000
  = 18_446_744_069_609_551_614   ← fits in u64, no error

Correct result:  36_893_488_139_219_103_230 + 4_100_000_000  (overflows u64 → should error)
Returned result: 18_446_744_069_609_551_614                  (silently ~half the correct value)
```

A transaction sender submitting a DAO withdrawal with a near-`u64::MAX`-capacity cell under sufficiently grown AR would receive this silently truncated capacity from the RPC, construct a valid-looking withdrawal transaction claiming less than their entitlement, and the node would accept it — permanently losing the difference.

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

**File:** util/dao/src/lib.rs (L202-205)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
        Ok(Capacity::shannons(reward))
```

**File:** util/dao/src/lib.rs (L242-245)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/dao/src/lib.rs (L256-258)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/src/lib.rs (L312-333)
```rust
    fn withdrawed_interests(
        &self,
        mut rtxs: impl Iterator<Item = &'a ResolvedTransaction> + Clone,
    ) -> Result<Capacity, DaoError> {
        let maximum_withdraws = rtxs.clone().try_fold(Capacity::zero(), |capacities, rtx| {
            self.transaction_maximum_withdraw(rtx)
                .and_then(|c| capacities.safe_add(c).map_err(Into::into))
        })?;
        let input_capacities = rtxs.try_fold(Capacity::zero(), |capacities, rtx| {
            let tx_input_capacities = rtx.resolved_inputs.iter().try_fold(
                Capacity::zero(),
                |tx_capacities, cell_meta| {
                    let output_capacity: Capacity = cell_meta.cell_output.capacity().into();
                    tx_capacities.safe_add(output_capacity)
                },
            )?;
            capacities.safe_add(tx_input_capacities)
        })?;
        maximum_withdraws
            .safe_sub(input_capacities)
            .map_err(Into::into)
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
