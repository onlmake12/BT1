### Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Produces Silently Wrong DAO Withdrawal Amount — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` in `util/dao/src/lib.rs` computes the interest-scaled withdrawal capacity using a u128 intermediate, but then casts the result back to u64 with the bare `as u64` operator (line 156). In Rust, `as u64` on a u128 value that exceeds `u64::MAX` silently wraps (takes the low 64 bits), producing a drastically smaller value with no error. Every other u128→u64 conversion in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, making this a clear inconsistency and a latent silent-corruption bug.

---

### Finding Description

In `util/dao/src/lib.rs`, the withdrawal calculation is:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
```

The multiplication is correctly widened to u128 to avoid overflow there. However, the result is cast back to u64 with `as u64`, which silently wraps if `withdraw_counted_capacity > u64::MAX`. The three other analogous calculations in the same file all use the checked pattern:

```rust
// secondary_block_reward (line 202-204)
let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
    / u128::from(target_parent_c.as_u64());
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// dao_field_with_current_epoch (line 242-245)
let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
    / u128::from(parent_c.as_u64());
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);

// dao_field_with_current_epoch (line 256-258)
let ar_increase128 =
    u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

The overflow condition for `withdraw_counted_capacity` is:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

`ar` (accumulation rate) starts at `10^16` and grows at roughly 4 % per year (secondary issuance ≈ 1.344 billion CKB/year against ~33.6 billion CKB total). For a cell holding the full realistic CKB supply (~3.36 × 10¹⁸ shannons), the ratio `withdrawing_ar / deposit_ar` needs to exceed ~5.5 for overflow to occur, which corresponds to ~43 years of chain operation. For smaller cells the threshold is proportionally higher. The condition is therefore reachable on a long-lived chain, and the existing test `check_withdraw_calculation_overflows` only exercises the path where `safe_add` catches the error afterward — it does not cover the silent-truncation path where the truncated value plus `occupied_capacity` still fits in u64, returning a silently wrong (far too small) result.

---

### Impact Explanation

`calculate_maximum_withdraw` is called from two paths:

1. **Consensus-critical path**: `transaction_maximum_withdraw` → `withdrawed_interests` → `dao_field_with_current_epoch` → `dao_field`. A silently wrong `withdrawed_interests` value propagates into the DAO field written into block headers. Nodes computing different DAO fields would disagree on block validity, causing a chain split.

2. **RPC path** (`calculate_dao_maximum_withdraw` in `rpc/src/module/experiment.rs` lines 259–267): Returns a silently wrong (too small) withdrawal amount to the caller, causing the user to construct a withdrawal transaction that claims less than they are entitled to — a direct loss of DAO interest for the depositor.

---

### Likelihood Explanation

The overflow requires `ar` to grow sufficiently relative to the deposit-time `ar`. At the current secondary issuance rate (~4 % annual `ar` growth), a cell holding a large fraction of the CKB supply would need to remain deposited for several decades before the condition is met. The likelihood is therefore **low** in the near term but **non-zero** on a long-lived chain, and the defect is already present in the code today. No attacker action is required — the condition arises purely from normal chain operation and large DAO deposits.

---

### Recommendation

Replace the silent cast on line 156 with the same checked pattern used everywhere else in the file:

```rust
// Before (silent truncation):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (consistent with the rest of the file):
let withdraw_counted = u64::try_from(withdraw_counted_capacity)
    .map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted).safe_add(occupied_capacity)?
```

Add a unit test that constructs a scenario where `counted_capacity * withdrawing_ar / deposit_ar` exceeds `u64::MAX` but `(result % 2^64) + occupied_capacity` does not, verifying that the function returns `Err(DaoError::Overflow)` rather than a silently wrong `Ok(...)`.

---

### Proof of Concept

The inconsistency is directly visible by comparing the four u128→u64 conversions in `util/dao/src/lib.rs`: [1](#0-0) 

(silent `as u64` cast — the defective line)

versus the three checked conversions in the same file: [2](#0-1) [3](#0-2) [4](#0-3) 

The existing overflow test only covers the case where `safe_add` raises the error, not the silent-truncation path: [5](#0-4) 

The RPC entry point that exposes this to any unprivileged caller: [6](#0-5)

### Citations

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L202-204)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
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

**File:** rpc/src/module/experiment.rs (L235-267)
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
```
