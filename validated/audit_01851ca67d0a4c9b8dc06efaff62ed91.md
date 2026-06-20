### Title
Silent Truncating Cast in `DaoCalculator::calculate_maximum_withdraw` Produces Wrong Withdrawal Amount — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` in `util/dao/src/lib.rs` uses a silent truncating `as u64` cast to convert the intermediate u128 result `withdraw_counted_capacity` into a `Capacity`. Every other analogous u128→u64 conversion in the same file uses the checked `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern. When `withdraw_counted_capacity` exceeds `u64::MAX`, the truncating cast silently produces a drastically wrong (much smaller) withdrawal amount instead of returning an error, causing incorrect fee computation and wrong RPC responses for DAO withdrawals.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the DAO withdrawal capacity as:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `withdraw_counted_capacity as u64` on line 156 is a **silent truncating cast**. In Rust, `as u64` on a `u128` value silently wraps modulo `2^64` when the value exceeds `u64::MAX`. This is the wrong implementation — it should be a checked conversion that propagates an overflow error.

Every other u128→u64 narrowing in the same file uses the correct checked pattern:

- `secondary_block_reward`: `let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;`
- `dao_field_with_current_epoch` (miner issuance): `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?`
- `dao_field_with_current_epoch` (ar increase): `let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;` [2](#0-1) [3](#0-2) [4](#0-3) 

The formula `counted_capacity * withdrawing_ar / deposit_ar` can exceed `u64::MAX` when `counted_capacity` is large and `withdrawing_ar / deposit_ar > 1` (which is always true since interest accumulates). For example, with `deposit_ar = 10^16`, `withdrawing_ar = 2 × 10^16` (a 2× accumulate rate, achievable over many years), and `counted_capacity = 10^19` shannons (~100 billion CKB), `withdraw_counted_capacity = 2 × 10^19 > u64::MAX ≈ 1.84 × 10^19`. The `as u64` cast silently truncates this to `≈ 1.55 × 10^18`, a value roughly 13× smaller than correct.

The existing overflow test `check_withdraw_calculation_overflows` only exercises the case where the final `safe_add(occupied_capacity)` overflows — it does not cover the case where `withdraw_counted_capacity` itself overflows u64 but the truncated value plus `occupied_capacity` does not, leaving the silent wrong-value path untested. [5](#0-4) 

---

### Impact Explanation

`calculate_maximum_withdraw` is called in two production paths:

**1. Transaction fee computation in the tx-pool** (`transaction_maximum_withdraw` → `transaction_fee` → `check_tx_fee`): [6](#0-5) [7](#0-6) 

When `withdraw_counted_capacity` silently truncates, `transaction_fee` computes `wrong_small_value - outputs_capacity`. If the truncated value is smaller than `outputs_capacity`, the subtraction underflows and the transaction is rejected with a misleading `DaoError`. If the truncated value is still larger than `outputs_capacity`, the fee is computed as a wrong (too small) amount, potentially causing the transaction to be rejected for low fee rate.

**2. The `calculate_dao_maximum_withdraw` RPC**: [8](#0-7) 

The RPC returns a silently wrong (much smaller) withdrawal capacity to the caller. Wallets and tools that rely on this RPC to construct DAO withdrawal transactions would build transactions with insufficient output capacity, causing them to fail on submission. This is the same class of impact as the reference report: the accounting function returns a wrong value that makes it impossible to correctly complete the operation.

---

### Likelihood Explanation

The condition requires `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. Since `counted_capacity ≤ u64::MAX` and `withdrawing_ar/deposit_ar > 1`, the overflow occurs when `counted_capacity > u64::MAX × deposit_ar / withdrawing_ar`. For a 2× accumulate rate ratio (achievable over many years of chain operation), `counted_capacity` must exceed ~`u64::MAX / 2 ≈ 9.2 × 10^18` shannons (~92 billion CKB). For a 1.1× ratio (10% total interest), the threshold is ~`u64::MAX / 1.1 ≈ 1.67 × 10^19` shannons (~167 billion CKB).

These are large but not impossible deposit sizes for institutional or protocol-level actors. The likelihood increases as the chain matures and the accumulate rate grows. An unprivileged DAO depositor triggers this path simply by submitting a withdrawal transaction or calling the RPC — no special privileges required.

---

### Recommendation

Replace the silent truncating cast with the same checked conversion used everywhere else in the file:

```rust
// Before (wrong):
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After (correct):
let withdraw_capacity = Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
)
.safe_add(occupied_capacity)?;
```

Add a test case where `withdraw_counted_capacity` itself overflows u64 but the truncated value plus `occupied_capacity` does not, to cover the silent-wrong-value path that the existing `check_withdraw_calculation_overflows` test misses.

---

### Proof of Concept

Concrete values that trigger the silent truncation (without triggering the `safe_add` overflow):

- `deposit_ar = 10_000_000_000_000_000` (genesis accumulate rate, 10^16)
- `withdrawing_ar = 20_000_000_000_000_000` (2× rate after many years)
- `output.capacity = 10_000_000_000_000_000_100` shannons (just above `u64::MAX / 2`)
- `occupied_capacity = 100` shannons (minimal lock script)
- `counted_capacity = 10_000_000_000_000_000_000`

Computation:
```
withdraw_counted_capacity = 10_000_000_000_000_000_000 * 20_000_000_000_000_000
                            / 10_000_000_000_000_000
                          = 20_000_000_000_000_000_000   // > u64::MAX (18_446_744_073_709_551_615)

withdraw_counted_capacity as u64
  = 20_000_000_000_000_000_000 % 2^64
  = 1_553_255_926_290_448_384   // silently wrong, ~13x too small

withdraw_capacity = 1_553_255_926_290_448_384 + 100
                  = 1_553_255_926_290_448_484   // returned without error, but wrong
```

The function returns `Ok(1_553_255_926_290_448_484)` instead of `Err(DaoError::Overflow)`. A depositor expecting ~`20 × 10^18` shannons receives a calculation result of ~`1.55 × 10^18` shannons, and any withdrawal transaction built on this value will be rejected by the DAO type script on-chain. [9](#0-8)

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

**File:** util/dao/src/lib.rs (L258-258)
```rust
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
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

**File:** tx-pool/src/util.rs (L28-53)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
    Ok(fee)
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
