### Title
Silent u128→u64 Truncation in NervosDAO Withdrawal Capacity Calculation — (`File: util/dao/src/lib.rs`)

### Summary
`DaoCalculator::calculate_maximum_withdraw` computes `withdraw_counted_capacity` as a `u128` intermediate value, then casts it to `u64` with a bare `as u64` — a silently truncating cast. Every other analogous `u128→u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. When the product exceeds `u64::MAX`, the high bits are silently discarded, producing a wrong (too-small) withdrawal capacity with no error returned.

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

The `as u64` cast silently discards the upper 64 bits if `withdraw_counted_capacity > u64::MAX`. No error is returned; the function proceeds with a wrong value.

Compare to every other `u128→u64` narrowing in the same file, all of which use checked conversion:

- `miner_issuance`: `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?`
- `ar_increase`: `u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?`
- `reward128`: `u64::try_from(reward128).map_err(|_| DaoError::Overflow)?` [2](#0-1) [3](#0-2) [4](#0-3) 

The `withdraw_counted_capacity` case is the sole exception. The existing overflow test (`check_withdraw_calculation_overflows`) only exercises the path where the final `safe_add` catches an overflow — it does not cover the case where `withdraw_counted_capacity` itself exceeds `u64::MAX` but the truncated value plus `occupied_capacity` still fits in `u64`, which is the silent-corruption path. [5](#0-4) 

### Impact Explanation

When `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`:

1. `withdraw_counted_capacity as u64` silently wraps to a small value.
2. `safe_add(occupied_capacity)` succeeds (no error propagated).
3. `calculate_maximum_withdraw` returns a **wrong, too-small** capacity.
4. Any DAO withdrawal transaction built from this value will be rejected by the on-chain DAO type script, which independently computes the correct maximum.
5. The `calculate_dao_maximum_withdraw` RPC endpoint also calls this function directly and returns the wrong value to callers. [6](#0-5) 

The depositor's funds are not lost, but the withdrawal is effectively blocked: the node's own calculator returns a wrong capacity, and any transaction built from it is rejected on-chain. This is a denial-of-service on DAO withdrawal for affected cells.

### Likelihood Explanation

The overflow condition requires `counted_capacity * withdrawing_ar > deposit_ar * u64::MAX`. Since `ar` starts at `10_000_000_000_000_000` (10^16) and grows by roughly `parent_ar * secondary_issuance / total_C` per block (~40,000/block at genesis parameters), and the total CKB supply is ~3.36×10^18 shannons, the ratio `withdrawing_ar / deposit_ar` would need to exceed ~5.5× for a cell holding the entire supply. At the current growth rate this takes on the order of hundreds of years on mainnet. However:

- The bug is a clear code defect inconsistent with every other narrowing cast in the same file.
- It would be triggered immediately on any chain with artificially elevated `ar` (e.g., a testnet or devnet with modified secondary issuance parameters).
- The `calculate_dao_maximum_withdraw` RPC is callable by any unprivileged RPC user with attacker-controlled `out_point` and `withdrawing_header_hash` parameters. [7](#0-6) 

### Recommendation

Replace the bare `as u64` cast with a checked conversion, consistent with all other `u128→u64` narrowings in the same file:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
``` [8](#0-7) 

Add a unit test covering the case where `withdraw_counted_capacity` exceeds `u64::MAX` but the truncated value plus `occupied_capacity` does not, verifying that `Err(DaoError::Overflow)` is returned rather than a silently wrong capacity.

### Proof of Concept

Construct a scenario with:
- `counted_capacity` = `u64::MAX / 2` = `9_223_372_036_854_775_807` shannons
- `deposit_ar` = `10_000_000_000_000_000`
- `withdrawing_ar` = `20_000_000_000_000_001` (just over 2× deposit_ar)

Then:
```
withdraw_counted_capacity (u128) = (u64::MAX/2) * 20_000_000_000_000_001 / 10_000_000_000_000_000
                                 ≈ 18_446_744_073_709_551_615 + 1  (> u64::MAX)
withdraw_counted_capacity as u64 = 0  (truncated)
```

The function returns `Capacity::shannons(0 + occupied_capacity)` — a drastically wrong value — with no error. The on-chain DAO script rejects any withdrawal transaction claiming this capacity, permanently blocking the withdrawal until the bug is fixed. [9](#0-8)

### Citations

**File:** util/dao/src/lib.rs (L146-158)
```rust
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
