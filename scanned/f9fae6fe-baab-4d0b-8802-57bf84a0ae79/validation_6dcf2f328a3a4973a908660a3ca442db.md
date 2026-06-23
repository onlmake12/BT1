### Title
Silent u128→u64 Truncation in NervosDAO Withdrawal Capacity Calculation Produces Incorrect Withdrawal Amount — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the maximum withdrawable capacity using a `u128` intermediate value, then converts it to `u64` with a bare `as u64` cast. This is a silent truncating cast: if the intermediate result exceeds `u64::MAX`, the upper 64 bits are silently discarded and the function returns an incorrect (too-small) capacity without any error. Every other analogous `u128→u64` conversion in the same file uses the checked `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern. The inconsistency is the root cause.

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

The `as u64` cast on line 156 silently discards the upper 64 bits of `withdraw_counted_capacity` if it exceeds `u64::MAX`. No error is returned; the function proceeds with a wrong value.

Compare this to every other `u128→u64` narrowing in the same file, all of which use the checked form:

```rust
// Line 244-245
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);

// Line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;

// Line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) 

The formula for `withdraw_counted_capacity` is:

```
counted_capacity × withdrawing_ar / deposit_ar
```

where `counted_capacity ≤ u64::MAX` and `withdrawing_ar > deposit_ar` (the accumulation rate `ar` is monotonically increasing). The product `counted_capacity × withdrawing_ar` is computed in `u128` (no panic), but the final quotient can exceed `u64::MAX` when `counted_capacity` is large and `withdrawing_ar/deposit_ar` is sufficiently above 1. In that case, `withdraw_counted_capacity as u64` wraps to a small value, and `safe_add(occupied_capacity)` succeeds silently, returning a drastically incorrect (too-small) capacity.

The existing overflow test (`check_withdraw_calculation_overflows`) only exercises the path where `safe_add` catches the overflow after the cast; it does **not** test the silent-truncation path where the cast itself produces a wrong value that `safe_add` then accepts. [3](#0-2) 

---

### Impact Explanation

`calculate_maximum_withdraw` is called in three places:

1. **`transaction_maximum_withdraw` → `withdrawed_interests` → `dao_field_with_current_epoch`**: The DAO field embedded in every block header is consensus-critical. If `withdrawed_interests` is computed from a silently truncated withdrawal capacity, `current_s` (the NervosDAO savings pool accumulator) is inflated. All nodes must agree on this field; a node computing it incorrectly would diverge from consensus. [4](#0-3) 

2. **`transaction_fee`**: The fee for a DAO withdrawal transaction is `maximum_withdraw - outputs_capacity`. A silently truncated `maximum_withdraw` makes the fee appear negative (underflow → `DaoError`), causing valid DAO withdrawal transactions to be incorrectly rejected by the tx-pool and block verifier. [5](#0-4) 

3. **RPC `calculate_dao_maximum_withdraw`**: Any RPC caller receives a silently wrong withdrawal amount, causing users to construct transactions with incorrect output capacities. [6](#0-5) 

---

### Likelihood Explanation

The truncation requires `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`. Since `ar` starts at `10^16` and grows slowly via secondary issuance, the ratio `withdrawing_ar/deposit_ar` would need to exceed approximately 5× for a cell holding the maximum realistic CKB balance. This is a long-horizon condition (decades at current issuance rates) under normal operation. However:

- The condition is reachable in test/devnet environments with manipulated `ar` values.
- The code defect is demonstrably present and inconsistent with the rest of the file.
- The absence of a checked cast means there is no defense-in-depth if `ar` growth assumptions are violated.

---

### Recommendation

Replace the bare `as u64` cast with the checked conversion already used everywhere else in the file:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

Add a test case where `withdraw_counted_capacity` itself (before `safe_add`) exceeds `u64::MAX` and verify that `DaoError::Overflow` is returned rather than a silently wrong value.

---

### Proof of Concept

Given the formula and the existing test scaffolding in `util/dao/src/tests.rs`:

```
deposit_ar     = 10_000_000_000_000_000   (10^16, genesis value)
withdrawing_ar = 55_000_000_000_000_000   (5.5× growth — reachable in devnet)
counted_capacity = 18_000_000_000_000_000_000  (18 × 10^18 shannons, near u64::MAX)

withdraw_counted_capacity (u128) = 18_000_000_000_000_000_000 × 55_000_000_000_000_000
                                   / 10_000_000_000_000_000
                                 = 99_000_000_000_000_000_000   (≈ 9.9 × 10^19)

u64::MAX = 18_446_744_073_709_551_615

99_000_000_000_000_000_000 > u64::MAX  →  truncation occurs

withdraw_counted_capacity as u64 = 99_000_000_000_000_000_000 mod 2^64
                                 = 99_000_000_000_000_000_000 - 5 × 2^64
                                 ≈ 6_766_279_631_452_241_920   (a small, wrong value)
```

`safe_add(occupied_capacity)` succeeds, and `calculate_maximum_withdraw` returns a capacity of approximately 6.77×10^18 shannons instead of the correct 9.9×10^19 shannons — a silent, undetected error of more than 10× in the withdrawal amount. [7](#0-6)

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

**File:** util/dao/src/lib.rs (L242-258)
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
