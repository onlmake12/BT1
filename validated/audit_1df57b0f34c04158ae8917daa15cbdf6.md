### Title
Silent Truncating Cast in NervosDAO Withdrawal Capacity Calculation Produces Incorrect Results - (File: `util/dao/src/lib.rs`)

### Summary

`DaoCalculator::calculate_maximum_withdraw` uses an unchecked `as u64` truncating cast on the result of a u128 intermediate computation. When the intermediate value exceeds `u64::MAX`, the cast silently wraps, returning a drastically wrong (too-small) withdrawal capacity without any error. Every other analogous u128→u64 narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, making this a clear inconsistency with a concrete correctness impact.

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the interest-bearing withdrawal amount as:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncating cast
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast silently truncates any value above `u64::MAX`. In contrast, every other u128→u64 narrowing in the same file uses the checked form:

```rust
// line 244-245
u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?
// line 258
u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?
``` [2](#0-1) 

The accumulation rate (`ar`) starts at `DEFAULT_GENESIS_ACCUMULATE_RATE = 10_000_000_000_000_000` (10^16) and grows monotonically. [3](#0-2) 

The formula is `withdraw_counted_capacity = counted_capacity × withdrawing_ar / deposit_ar`. When `withdrawing_ar / deposit_ar > 1` (always true for any accrued interest) and `counted_capacity` is large, the numerator `counted_capacity × withdrawing_ar` can exceed `u64::MAX` after the division. For example:

- `counted_capacity = u64::MAX / 2 ≈ 9.22 × 10^18` shannons
- `withdrawing_ar = 2 × deposit_ar` (AR has doubled)
- `withdraw_counted_capacity ≈ u64::MAX` — borderline overflow
- With slightly larger values, `as u64` wraps to a tiny number
- `Capacity::shannons(tiny).safe_add(occupied_capacity)` succeeds silently, returning a wrong result

The existing test `check_withdraw_calculation_overflows` only catches the case where the truncated value plus `occupied_capacity` still overflows u64 (caught by `safe_add`). It does **not** cover the case where the truncated value is small enough that `safe_add` succeeds, producing a silently wrong result. [4](#0-3) 

### Impact Explanation

Two reachable code paths are affected:

1. **`transaction_fee` → `transaction_maximum_withdraw` → `calculate_maximum_withdraw`**: Called during tx-pool admission (`check_tx_fee`) and block verification. A silently wrong (too-small) `maximum_withdraw` causes `maximum_withdraw.safe_sub(outputs_capacity)` to fail, incorrectly rejecting a valid DAO withdrawal transaction. Alternatively, if the truncated value is larger than `outputs_capacity`, the fee is computed incorrectly, potentially allowing a zero-fee DAO withdrawal to pass the fee check. [5](#0-4) 

2. **`calculate_dao_maximum_withdraw` RPC**: Returns a wrong capacity value to any RPC caller, causing users to construct DAO withdrawal transactions with incorrect output capacities. [6](#0-5) 

### Likelihood Explanation

The AR grows at approximately the secondary issuance rate relative to total capacity (~1.344 billion CKB/year secondary issuance vs. ~33.6 billion CKB total ≈ 4% annual AR growth). For AR to double from its genesis value, roughly 25 years of chain operation would be required. However:

- The vulnerability is a **silent** wrong result (not an error), meaning it would be undetected until triggered.
- The `as u64` cast is inconsistent with every other narrowing in the same file, indicating it is an unintentional omission.
- The `calculate_dao_maximum_withdraw` RPC is directly callable by any unprivileged RPC user with attacker-controlled `out_point` and `withdrawing_header_hash` parameters. [7](#0-6) 

### Recommendation

Replace the silent truncating cast with the same checked conversion pattern used elsewhere in the file:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (safe, consistent with lines 244-245 and 258):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?
``` [1](#0-0) 

### Proof of Concept

The following values demonstrate the silent truncation path (not caught by the existing overflow test):

```
deposit_ar      = 10_000_000_000_000_000   (genesis AR)
withdrawing_ar  = 20_000_000_000_000_001   (AR doubled + 1, ~25 years of accrual)
counted_capacity = 9_223_372_036_854_775_808  (u64::MAX / 2)

withdraw_counted_capacity (u128) =
    9_223_372_036_854_775_808 × 20_000_000_000_000_001 / 10_000_000_000_000_000
  = 18_446_744_073_709_551_617   (> u64::MAX by 2)

withdraw_counted_capacity as u64 = 1   (silent wrap-around)

withdraw_capacity = Capacity::shannons(1).safe_add(occupied_capacity)
                  ≈ occupied_capacity   (≈ 6_100_000_000 shannons)
```

The depositor who locked ~92 billion CKB receives back only ~61 CKB worth of capacity. The function returns `Ok(...)` with no error, and the existing `check_withdraw_calculation_overflows` test does not cover this case because it only tests values where the final `safe_add` catches the overflow. [8](#0-7) [9](#0-8)

### Citations

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

**File:** util/dao/utils/src/lib.rs (L16-17)
```rust
// This is multiplied by 10**16 to make sure we have enough precision.
const DEFAULT_GENESIS_ACCUMULATE_RATE: u64 = 10_000_000_000_000_000;
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
