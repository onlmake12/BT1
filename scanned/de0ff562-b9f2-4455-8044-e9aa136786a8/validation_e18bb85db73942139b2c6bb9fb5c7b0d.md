### Title
Silent `u128`→`u64` Truncation in `calculate_maximum_withdraw` Causes Incorrect DAO Field Accounting — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` uses an unchecked `as u64` cast on a `u128` intermediate result. Every other analogous computation in the same file uses `u64::try_from(…).map_err(|_| DaoError::Overflow)?`. When the intermediate value exceeds `u64::MAX`, the cast silently truncates instead of returning an error, producing a wrong (too-small) withdrawal amount. Because this function feeds into `withdrawed_interests`, which feeds into `dao_field_with_current_epoch`, the block-level DAO field `S` (NervosDAO surplus) is silently inflated. All nodes compute the same wrong value, so the inflated field is accepted by consensus.

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

Every other `u128`→`u64` narrowing in the same file uses the checked form:

```rust
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) 

```rust
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
``` [3](#0-2) 

The `DaoError::Overflow` variant exists precisely for this purpose: [4](#0-3) 

`calculate_maximum_withdraw` is called from `transaction_maximum_withdraw`, which is called from `withdrawed_interests`, which is called from `dao_field_with_current_epoch`: [5](#0-4) 

`dao_field_with_current_epoch` uses `withdrawed_interests` to compute `current_s`:

```rust
let current_s = parent_s
    .safe_add(nervosdao_issuance)
    .and_then(|s| s.safe_sub(withdrawed_interests))?;
``` [6](#0-5) 

If `withdraw_counted_capacity` silently truncates, `maximum_withdraw` is too small → `withdrawed_interests` is too small → `current_s` is inflated. The block assembler writes this inflated `S` into the block DAO field: [7](#0-6) 

The contextual block verifier recomputes the DAO field using the same function and compares it to the block header: [8](#0-7) 

Because both sides use the same buggy `calculate_maximum_withdraw`, the inflated `S` passes verification. The DAO surplus accounting is permanently wrong from that block onward.

---

### Impact Explanation

- The NervosDAO surplus field `S` in the block header is inflated by the amount of truncated interest.
- Future DAO depositors receive slightly more interest than the protocol intends (drawn from a phantom surplus).
- The error is consensus-accepted and irreversible once committed to the chain.
- The RPC `calculate_dao_maximum_withdraw` also returns a silently wrong (too-small) value to users, causing them to construct withdrawal transactions with incorrect output capacities. [9](#0-8) 

---

### Likelihood Explanation

The truncation requires `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX ≈ 1.84 × 10¹⁹`. The total CKB supply is ~3.36 × 10¹⁸ shannons, so `counted_capacity` is bounded below that. The ratio `withdrawing_ar / deposit_ar` must therefore exceed ~5.5× for the product to overflow. Since `ar` starts at `10¹⁶` and grows proportionally to secondary issuance divided by total capacity, reaching a 5.5× multiple would take many decades under current parameters. Likelihood is low but non-zero over a long enough time horizon, and the bug is present in the code today.

The existing test `check_withdraw_calculation_overflows` only exercises the `safe_add` overflow path (very large `output.capacity()`), not the `as u64` truncation path: [10](#0-9) 

---

### Recommendation

Replace the silent cast with the checked conversion already used elsewhere in the same file:

```rust
// Before (line 155-156):
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After:
let withdraw_capacity = Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
).safe_add(occupied_capacity)?;
```

Add a unit test that constructs a scenario where `counted_capacity * withdrawing_ar / deposit_ar` exceeds `u64::MAX` and asserts `DaoError::Overflow` is returned.

---

### Proof of Concept

1. Construct a DAO deposit cell with `counted_capacity` near `u64::MAX / 2` (e.g., `9 × 10¹⁸` shannons).
2. Wait (or simulate) until `withdrawing_ar / deposit_ar ≥ 2.05`, so `withdraw_counted_capacity > u64::MAX`.
3. Call `DaoCalculator::calculate_maximum_withdraw` with the deposit and withdrawing headers.
4. Observe: instead of `DaoError::Overflow`, the function returns `Ok(Capacity::shannons(truncated_value))` where `truncated_value = (withdraw_counted_capacity & 0xFFFFFFFFFFFFFFFF) as u64` — a value far smaller than the correct withdrawal amount.
5. Include a DAO withdrawal transaction using this cell in a block. The block assembler computes `withdrawed_interests` using the truncated value, producing an inflated `current_s`. The verifier accepts the block because it recomputes the same inflated value. [11](#0-10)

### Citations

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

**File:** util/dao/src/lib.rs (L252-254)
```rust
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;
```

**File:** util/dao/src/lib.rs (L258-258)
```rust
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

**File:** util/dao/utils/src/error.rs (L36-38)
```rust
    /// Calculation overflow
    #[error("Overflow")]
    Overflow,
```

**File:** tx-pool/src/block_assembler/mod.rs (L677-678)
```rust
        let dao = DaoCalculator::new(consensus, &snapshot.borrow_as_data_loader())
            .dao_field_with_current_epoch(entries_iter, tip_header, current_epoch)?;
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L300-319)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let dao = DaoCalculator::new(
            &self.context.consensus,
            &self.context.store.borrow_as_data_loader(),
        )
        .dao_field(self.resolved.iter().map(AsRef::as_ref), self.parent)
        .map_err(|e| {
            error_target!(
                crate::LOG_TARGET,
                "Error generating dao data for block {}: {:?}",
                self.header.hash(),
                e
            );
            e
        })?;

        if dao != self.header.dao() {
            return Err((BlockErrorKind::InvalidDAO).into());
        }
        Ok(())
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
