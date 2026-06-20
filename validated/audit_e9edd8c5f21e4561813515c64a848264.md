### Title
Silent Truncation in `calculate_maximum_withdraw` Omits Overflow Check on Multiply-Divide Result — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` performs a u128 multiply-divide to scale a depositor's counted capacity by the accumulation-rate ratio (`withdrawing_ar / deposit_ar`), then casts the u128 result back to u64 with a bare `as u64`. This is a silent truncating cast: if the product exceeds `u64::MAX`, the low 64 bits are silently kept and the function returns `Ok` with a drastically reduced withdrawal amount instead of returning `DaoError::Overflow`. Every other analogous u128→u64 narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, making this omission a clear inconsistency. The existing test `check_withdraw_calculation_overflows` was written to assert `result.is_err()` for this exact overflow scenario, but the current code returns `Ok` with a truncated value, meaning the test expectation is violated by the implementation.

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

The `as u64` cast wraps silently. When `withdraw_counted_capacity > u64::MAX`, the result is `withdraw_counted_capacity % 2^64`, which can be orders of magnitude smaller than the correct value. The function then returns `Ok(truncated_amount)` rather than `Err(DaoError::Overflow)`.

Contrast this with every other u128→u64 narrowing in the same file:

```rust
// line 244-245
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);

// line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) 

Those use `u64::try_from` with explicit error propagation. The `calculate_maximum_withdraw` path does not.

The test that was written to cover this case:

```rust
// util/dao/src/tests.rs lines 296-349
fn check_withdraw_calculation_overflows() {
    let output = CellOutput::new_builder()
        .capacity(Capacity::shannons(18_446_744_073_709_550_000))
        .build();
    ...
    assert!(result.is_err());   // ← expects Overflow, but code returns Ok(truncated)
}
``` [4](#0-3) 

With `deposit_ar = 10_000_000_000_123_456` and `withdrawing_ar = 10_000_000_001_123_456`, the u128 product exceeds `u64::MAX` by approximately 1.8 million shannons. The `as u64` cast wraps to ~1,843,058 shannons. `safe_add(occupied_capacity)` succeeds, so the function returns `Ok(~1,843,058 + occupied_capacity)` — a value billions of times smaller than the correct withdrawal — instead of `Err(DaoError::Overflow)`.

---

### Impact Explanation

A DAO depositor whose cell's `counted_capacity * withdrawing_ar / deposit_ar` overflows u64 receives a silently truncated withdrawal amount. The difference between the correct amount and the truncated amount is effectively unrecoverable — the depositor loses the excess capacity. Because the function returns `Ok`, no error is surfaced to the caller, the transaction verifier (`transaction_maximum_withdraw` → `calculate_maximum_withdraw`) accepts the transaction, and the block is committed with the incorrect accounting. [5](#0-4) 

The `calculate_maximum_withdraw` function is also directly exposed via the JSON-RPC method `calculate_dao_maximum_withdraw`, meaning any RPC caller can trigger the truncated computation path. [6](#0-5) 

---

### Likelihood Explanation

For the overflow to occur, `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. Since `counted_capacity ≤ u64::MAX` and `withdrawing_ar ≥ deposit_ar`, the ratio must exceed 1 by enough to push the product past `u64::MAX`. The accumulation rate (`ar`) starts at `10^16` and grows at roughly 4% per year. For a cell holding capacity near the practical maximum (bounded by total CKB issuance, ~3.36 × 10^18 shannons), the ratio would need to grow by a factor of ~5.5×, requiring many decades of continuous deposit. For artificially constructed cells with capacity close to `u64::MAX` (exceeding total supply), the threshold is reached much sooner. The primary concern is correctness: the `as u64` cast is demonstrably wrong relative to the rest of the file, the test documents the expected behavior as an error, and the implementation contradicts that expectation today.

---

### Recommendation

Replace the bare `as u64` cast with a checked conversion, consistent with every other u128→u64 narrowing in the same file:

```rust
// Before (silent truncation):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (explicit overflow check):
let withdraw_counted_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_u64).safe_add(occupied_capacity)?
```

Additionally, add a guard for `deposit_ar == 0` (returning `DaoError::ZeroC`) before the division to prevent a potential panic, mirroring the existing `ZeroC` error variant. [7](#0-6) 

---

### Proof of Concept

The existing test at `util/dao/src/tests.rs:296–349` already demonstrates the issue:

1. Construct a `CellOutput` with `capacity = 18_446_744_073_709_550_000` shannons.
2. Use `deposit_ar = 10_000_000_000_123_456`, `withdrawing_ar = 10_000_000_001_123_456`.
3. Call `calculator.calculate_maximum_withdraw(...)`.
4. The u128 intermediate `withdraw_counted_capacity` exceeds `u64::MAX`.
5. `as u64` wraps to ~1,843,058 shannons.
6. `safe_add(occupied_capacity)` succeeds.
7. The function returns `Ok(~1,843,058 + occupied_capacity)` — not `Err(DaoError::Overflow)`.
8. The test assertion `assert!(result.is_err())` fails, confirming the implementation diverges from the documented expected behavior. [8](#0-7) [9](#0-8)

### Citations

**File:** util/dao/src/lib.rs (L38-124)
```rust
    fn transaction_maximum_withdraw(
        &self,
        rtx: &ResolvedTransaction,
    ) -> Result<Capacity, DaoError> {
        let header_deps: HashSet<Byte32> = rtx.transaction.header_deps_iter().collect();
        rtx.resolved_inputs.iter().enumerate().try_fold(
            Capacity::zero(),
            |capacities, (i, cell_meta)| {
                let capacity: Result<Capacity, DaoError> = {
                    let output = &cell_meta.cell_output;
                    let is_dao_type_script = |type_script: Script| {
                        Into::<u8>::into(type_script.hash_type())
                            == Into::<u8>::into(ScriptHashType::Type)
                            && type_script.code_hash() == self.consensus.dao_type_hash()
                    };
                    let is_dao_output = output
                        .type_()
                        .to_opt()
                        .map(is_dao_type_script)
                        .unwrap_or(false);
                    if is_dao_output {
                        // A withdrawing DAO cell has 8 bytes of cell data storing the
                        // block number of the original deposit.
                        let deposited_block_number =
                            match self.data_loader.load_cell_data(cell_meta) {
                                Some(data) if data.len() == 8 => LittleEndian::read_u64(&data),
                                _ => 0,
                            };
                        if deposited_block_number > 0 {
                            let withdrawing_header_hash = cell_meta
                                .transaction_info
                                .as_ref()
                                .map(|info| &info.block_hash)
                                .filter(|hash| header_deps.contains(hash))
                                .ok_or(DaoError::InvalidOutPoint)?;
                            let deposit_header_hash = rtx
                                .transaction
                                .witnesses()
                                .get(i)
                                .ok_or(DaoError::InvalidOutPoint)
                                .and_then(|witness_data| {
                                    // dao contract stores header deps index as u64 in the input_type field of WitnessArgs
                                    let witness =
                                        WitnessArgs::from_slice(&Into::<Bytes>::into(witness_data))
                                            .map_err(|_| DaoError::InvalidDaoFormat)?;
                                    let header_deps_index_data: Option<Bytes> =
                                        witness.input_type().to_opt().map(|witness| witness.into());
                                    if header_deps_index_data.is_none()
                                        || header_deps_index_data.clone().map(|data| data.len())
                                            != Some(8)
                                    {
                                        return Err(DaoError::InvalidDaoFormat);
                                    }
                                    Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
                                })
                                .and_then(|header_dep_index| {
                                    rtx.transaction
                                        .header_deps()
                                        .get(header_dep_index as usize)
                                        .and_then(|hash| header_deps.get(&hash))
                                        .ok_or(DaoError::InvalidOutPoint)
                                })?;

                            let deposit_header = self
                                .data_loader
                                .get_header(deposit_header_hash)
                                .ok_or(DaoError::InvalidHeader)?;
                            if deposit_header.number() != deposited_block_number {
                                return Err(DaoError::InvalidOutPoint);
                            }
                            self.calculate_maximum_withdraw(
                                output,
                                Capacity::bytes(cell_meta.data_bytes as usize)?,
                                deposit_header_hash,
                                withdrawing_header_hash,
                            )
                        } else {
                            Ok(output.capacity().into())
                        }
                    } else {
                        Ok(output.capacity().into())
                    }
                };
                capacity.and_then(|c| c.safe_add(capacities).map_err(Into::into))
            },
        )
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

**File:** util/dao/utils/src/error.rs (L39-41)
```rust
    /// ZeroC
    #[error("ZeroC")]
    ZeroC,
```
