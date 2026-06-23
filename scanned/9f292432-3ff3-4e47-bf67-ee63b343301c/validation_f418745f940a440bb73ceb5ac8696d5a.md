### Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Returns Wrong Withdrawal Amount Without Error — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the NervosDAO withdrawal capacity using a `u128` intermediate value, then casts it to `u64` with a bare `as u64` — a silent truncating cast. When the intermediate result exceeds `u64::MAX`, the function silently returns a drastically wrong (bit-truncated) capacity and propagates `Ok(wrong_amount)` to every caller, including the public `calculate_dao_maximum_withdraw` RPC, the `transaction_fee` calculator, and the `withdrawed_interests` DAO-field updater. No error is raised and no caller is notified of the shortfall.

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

The `as u64` cast silently discards the high 64 bits of the `u128` result. Every other analogous u128→u64 narrowing in the same file uses the checked `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern:

- `secondary_block_reward`: `let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;`
- `dao_field_with_current_epoch` (miner issuance): `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?`
- `dao_field_with_current_epoch` (ar increase): `let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;` [2](#0-1) [3](#0-2) [4](#0-3) 

`calculate_maximum_withdraw` is the sole exception. The `DaoError::Overflow` variant exists precisely for this purpose but is never triggered here. [5](#0-4) 

The function is called from three production paths:

1. **`transaction_maximum_withdraw` → `transaction_fee`** — used by the tx-pool to compute DAO withdrawal fees. [6](#0-5) 

2. **`transaction_maximum_withdraw` → `withdrawed_interests` → `dao_field_with_current_epoch`** — used during block assembly to update the on-chain DAO accumulator field `current_s`. [7](#0-6) 

3. **`calculate_dao_maximum_withdraw` RPC** — the public JSON-RPC endpoint that wallets and users call to learn how much they can withdraw. [8](#0-7) 

---

### Impact Explanation

When `withdraw_counted_capacity` overflows `u64::MAX`, the `as u64` cast wraps to a small value (low 64 bits of the u128). The function returns `Ok(tiny_wrong_capacity)` instead of `Err(DaoError::Overflow)`.

**Concrete numeric example:**
- `deposit_ar = 10_000_000_000_000_000` (genesis accumulate rate, ~10¹⁶)
- `withdrawing_ar = 200_000_000_000_000_000` (after decades of secondary issuance, ~2×10¹⁷; a 20× growth)
- `counted_capacity = 1_000_000_000_000_000_000` shannons (~10¹⁰ CKB, a large but valid deposit)

```
withdraw_counted_capacity = 10¹⁸ × 2×10¹⁷ / 10¹⁶ = 2×10¹⁹
u64::MAX                  = 1.844×10¹⁹
```

`2×10¹⁹ as u64 = 2×10¹⁹ − 2⁶⁴ ≈ 1.56×10¹⁸` — roughly **12.8× less** than the correct value.

The function returns `Ok(Capacity::shannons(1.56×10¹⁸))` silently. Downstream effects:

- **RPC callers** receive a drastically understated withdrawal amount; wallets construct transactions with wrong output capacity, which the on-chain DAO script rejects.
- **`transaction_fee`** computes a wrong (inflated) fee for DAO withdrawal transactions, potentially causing valid transactions to be rejected from the tx-pool or accepted with incorrect fee accounting.
- **`dao_field_with_current_epoch`** subtracts a wrong `withdrawed_interests` value from `current_s`, silently corrupting the DAO accumulator state written into every subsequent block header. [9](#0-8) 

---

### Likelihood Explanation

The overflow condition requires `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`. The accumulate rate `ar` starts at `10_000_000_000_000_000` (~10¹⁶) at genesis and grows with each block's secondary issuance. The secondary epoch reward is 1.344 billion CKB/year initially. Over decades the ratio `withdrawing_ar / deposit_ar` can grow substantially. A depositor who locks a large amount (e.g., billions of CKB) near genesis and withdraws after many decades crosses the overflow threshold. The entry path requires no privilege: any RPC caller can trigger the RPC path, and any transaction sender submitting a DAO withdrawal triggers the fee/DAO-field paths. The existing test `check_withdraw_calculation_overflows` inadvertently masks this bug — it passes only because the subsequent `safe_add` catches a different overflow, not the `as u64` truncation itself. [10](#0-9) 

---

### Recommendation

Replace the bare `as u64` cast with the same checked conversion used everywhere else in the file:

```rust
// Before (silent truncation):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (checked, consistent with the rest of the file):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?
``` [11](#0-10) 

---

### Proof of Concept

The following values demonstrate the silent truncation:

```
deposit_ar      = 10_000_000_000_000_000   // genesis ar
withdrawing_ar  = 200_000_000_000_000_000  // ar after decades
counted_capacity = 1_000_000_000_000_000_000 shannons

withdraw_counted_capacity (u128) = 1_000_000_000_000_000_000
                                   × 200_000_000_000_000_000
                                   / 10_000_000_000_000_000
                                 = 20_000_000_000_000_000_000  (> u64::MAX = 18_446_744_073_709_551_615)

withdraw_counted_capacity as u64 = 20_000_000_000_000_000_000
                                   - 18_446_744_073_709_551_616
                                 = 1_553_255_926_290_448_384   // ← silently wrong

calculate_maximum_withdraw returns Ok(1_553_255_926_290_448_384 + occupied_capacity)
instead of Err(DaoError::Overflow)
```

The caller receives a value ~12.8× smaller than correct, with no indication of error. The `calculate_dao_maximum_withdraw` RPC propagates this wrong value directly to the user. [12](#0-11)

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

**File:** util/dao/src/lib.rs (L152-158)
```rust
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

**File:** util/dao/src/lib.rs (L242-254)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
        let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;

        let current_c = parent_c.safe_add(current_g)?;
        let current_u = parent_u
            .safe_add(added_occupied_capacities)
            .and_then(|u| u.safe_sub(freed_occupied_capacities))?;
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;
```

**File:** util/dao/src/lib.rs (L256-259)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
        let current_ar = parent_ar
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

**File:** util/dao/utils/src/error.rs (L36-41)
```rust
    /// Calculation overflow
    #[error("Overflow")]
    Overflow,
    /// ZeroC
    #[error("ZeroC")]
    ZeroC,
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
