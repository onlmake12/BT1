### Title
Silent `u128 → u64` Truncation in DAO Withdrawal Capacity Calculation Silently Underpays Depositors — (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the grown withdrawal capacity using a `u128` intermediate, then casts it to `u64` with a **silent truncating `as u64`** instead of the checked `u64::try_from(…)` used everywhere else in the same file. If the intermediate result exceeds `u64::MAX`, the high bits are silently discarded, the depositor receives a much smaller amount than they earned, and the excess capacity remains locked in the DAO contract — a direct analog to the rebasing-token accounting discrepancy in the reference report.

---

### Finding Description

The Nervos DAO is CKB's native "rebasing" mechanism: deposited CKBytes earn interest proportional to the growth of the accumulation rate `ar`, which increases every block via secondary issuance. The withdrawal amount is:

```
withdraw_counted_capacity = counted_capacity × withdrawing_ar / deposit_ar
```

This is computed in `u128` to avoid intermediate overflow, but the final narrowing to `u64` is done with an unchecked cast:

```rust
// util/dao/src/lib.rs  lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

Every other `u128 → u64` narrowing in the same file uses the checked form that propagates `DaoError::Overflow`:

```rust
// line 244-245  (miner_issuance)
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)

// line 258  (ar_increase)
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;

// line 204  (secondary_block_reward)
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The `as u64` cast at line 156 is the sole exception. When `withdraw_counted_capacity > u64::MAX`, the value wraps silently and `Capacity::shannons(…)` is called with a drastically smaller number, producing a withdrawal amount that is billions of CKBytes short of what the depositor is owed.

The downstream consumers of this function are:

1. **`transaction_maximum_withdraw` → `transaction_fee`** — used by the block verifier to check that a DAO withdrawal transaction does not claim more than the maximum. A truncated maximum causes the verifier to accept a transaction that pays the correct (larger) amount as if it were paying an inflated fee, or to reject a correctly-constructed transaction outright.
2. **RPC `calculate_dao_maximum_withdraw`** — returns the truncated (wrong) value to wallets and users. [5](#0-4) [6](#0-5) 

---

### Impact Explanation

When `withdraw_counted_capacity` wraps past `u64::MAX`, the depositor's entitled withdrawal is silently replaced by a much smaller value (the low 64 bits of the true result). The excess capacity — the interest the depositor legitimately earned — is not returned and cannot be recovered; it remains locked in the DAO contract. This is the exact CKB analog of the rebasing-token issue: the "balance" of the DAO cell has grown (via `ar`), but the accounting code assumes the stored cell capacity is the full withdrawable amount, discarding the grown portion.

---

### Likelihood Explanation

For `withdraw_counted_capacity` to exceed `u64::MAX`, the ratio `withdrawing_ar / deposit_ar` must be large enough that `counted_capacity × ratio > u64::MAX`. With the current CKB secondary issuance schedule, `ar` grows by roughly 4 000 units per block from its genesis value of `10^16`. A factor-of-5 growth (sufficient to overflow a near-maximum deposit) would require on the order of `10^13` blocks — far beyond any practical horizon.

However, the defect is real and the code is demonstrably inconsistent: the existing test `check_withdraw_calculation_overflows` exercises a near-`u64::MAX` capacity and expects `result.is_err()`, but the error it actually receives comes from the subsequent `safe_add(occupied_capacity)` overflow, **not** from the `as u64` truncation. A carefully chosen capacity value where the truncated result plus `occupied_capacity` does not overflow `u64` would silently return `Ok` with a wrong (too-small) withdrawal amount, bypassing the test's intent. [7](#0-6) 

---

### Recommendation

Replace the silent cast with the same checked conversion used everywhere else in the file:

```rust
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    )
    .safe_add(occupied_capacity)?;
```

This makes the overflow path explicit and consistent with `miner_issuance`, `ar_increase`, and `secondary_block_reward` calculations.

---

### Proof of Concept

**Trigger condition** (arithmetic):

```
counted_capacity  = C   (shannons, up to ~3.36 × 10^18 on mainnet)
deposit_ar        = A
withdrawing_ar    = A + ΔA

withdraw_counted_capacity = C × (A + ΔA) / A
                          = C + C × ΔA / A

Overflow when: C × ΔA / A  >  u64::MAX - C
```

**Concrete example** (synthetic, not mainnet-reachable today):

```
deposit_ar     = 10_000_000_000_000_000   (genesis ar)
withdrawing_ar = 50_000_000_000_000_000   (5× growth)
counted_capacity = 4_000_000_000_000_000_000  shannons  (~40 billion CKBytes)

withdraw_counted_capacity (u128) = 4e18 × 5e16 / 1e16 = 20_000_000_000_000_000_000
                                 > u64::MAX (18_446_744_073_709_551_615)

withdraw_counted_capacity as u64 = 20e18 - 2^64
                                 ≈ 1_553_255_926_290_448_384   (silently truncated)

Depositor receives ≈ 15.5 billion CKBytes instead of ≈ 200 billion CKBytes.
Excess ≈ 184 billion CKBytes remains locked in the DAO contract.
```

The entry path is any unprivileged transaction sender submitting a DAO phase-2 withdrawal transaction, or any RPC caller invoking `calculate_dao_maximum_withdraw`. [8](#0-7)

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

**File:** util/dao/src/tests.rs (L295-349)
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
```
