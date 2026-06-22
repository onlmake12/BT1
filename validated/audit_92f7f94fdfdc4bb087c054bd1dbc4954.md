### Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Causes Permanent DAO Withdrawal Freeze - (File: util/dao/src/lib.rs)

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the interest-adjusted withdrawal capacity using a u128 intermediate value, then silently truncates it to u64 with an `as u64` cast. When the intermediate product exceeds `u64::MAX`, the truncated result can be **less than the original deposited capacity**, producing a "loss" the code never anticipates. Downstream callers that assume `maximum_withdraw >= input_capacity` then fail with `safe_sub`, permanently blocking the DAO withdrawal and causing block validation to reject any block that contains it.

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

The `as u64` cast is an **unchecked truncation**: if `withdraw_counted_capacity > u64::MAX`, the high bits are silently discarded, yielding a value that can be far smaller than `counted_capacity`. No `DaoError::Overflow` is returned; the wrong value propagates silently.

This result feeds directly into `withdrawed_interests`:

```rust
maximum_withdraws
    .safe_sub(input_capacities)
    .map_err(Into::into)
``` [2](#0-1) 

`safe_sub` returns `CapacityError::Overflow` (mapped to `DaoError::Overflow`) whenever `maximum_withdraws < input_capacities`. Because `calculate_maximum_withdraw` can silently produce a value smaller than the deposited capacity, this subtraction fails even for a fully legitimate withdrawal.

`withdrawed_interests` is called inside `dao_field_with_current_epoch`:

```rust
let current_s = parent_s
    .safe_add(nervosdao_issuance)
    .and_then(|s| s.safe_sub(withdrawed_interests))?;
``` [3](#0-2) 

A failure here propagates up through block validation, causing the entire block to be rejected. The same path is exercised in the tx-pool via `check_tx_fee`:

```rust
let fee = DaoCalculator::new(...)
    .transaction_fee(rtx)
    .map_err(|err| {
        Reject::Malformed(format!("{err}"), ...)
    })?;
``` [4](#0-3) 

A `Reject::Malformed` result permanently bans the transaction from the pool. Because the tx-pool and block verifier both call the same `DaoCalculator` path, the withdrawal is rejected at both layers with no recovery path.

The existing test `check_withdraw_calculation_overflows` only catches the case where `safe_add(occupied_capacity)` overflows after truncation; it does **not** cover the case where truncation silently produces a value smaller than `counted_capacity` that then passes `safe_add` but breaks the downstream `safe_sub`. [5](#0-4) 

### Impact Explanation

A DAO depositor whose cell triggers the truncation condition cannot withdraw their funds. The tx-pool permanently marks the withdrawal transaction as malformed (`Reject::Malformed`), and any block a miner assembles containing it fails `dao_field_with_current_epoch` validation. The deposited CKB is permanently frozen with no admin escape hatch in the on-chain protocol.

### Likelihood Explanation

The truncation condition requires `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`. With the initial `ar ≈ 10^16` and total CKB supply `≈ 3.36 × 10^18` shannons, the ratio must grow by roughly 5.5× before a maximum-supply cell triggers it — approximately 40+ years at current secondary issuance rates. However:

- The condition is reachable **today** with artificially constructed cells whose capacity is set near `u64::MAX` (valid on-chain since the protocol only requires `capacity ≥ occupied_capacity`).
- A script author or transaction sender can craft such a cell and submit it to the tx-pool via `send_transaction` RPC, triggering the malformed rejection path immediately.
- The `check_withdraw_calculation_overflows` unit test uses `capacity = 18_446_744_073_709_550_000` (close to `u64::MAX`) and already demonstrates the overflow boundary, confirming the path is reachable. [6](#0-5) 

### Recommendation

Replace the silent `as u64` cast with an explicit checked conversion that returns `DaoError::Overflow` on truncation, consistent with how the same pattern is handled elsewhere in the same function:

```rust
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity)
        .map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64)
        .safe_add(occupied_capacity)?;
```

This mirrors the existing overflow guard used for `ar_increase` and `miner_issuance` in `dao_field_with_current_epoch`. [7](#0-6) 

### Proof of Concept

1. Craft a DAO deposit cell with `capacity = u64::MAX - occupied_capacity + 1` (e.g., `18_446_744_073_709_551_555` shannons).
2. After any block is mined (so `withdrawing_ar > deposit_ar`), attempt Phase 2 withdrawal via `send_transaction` RPC.
3. `calculate_maximum_withdraw` computes `withdraw_counted_capacity` as a u128 value exceeding `u64::MAX`; the `as u64` cast wraps it to a small value.
4. `transaction_fee` calls `maximum_withdraw.safe_sub(outputs_capacity)` where `outputs_capacity` equals the original deposited amount; `safe_sub` fails.
5. `check_tx_fee` returns `Reject::Malformed`, permanently banning the transaction.
6. Any miner who manually includes the transaction in a block will have that block rejected by all peers via `dao_field_with_current_epoch` → `withdrawed_interests` → `safe_sub` failure.

The deposited CKB is permanently unrecoverable through the normal protocol path.

### Citations

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L252-254)
```rust
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;
```

**File:** util/dao/src/lib.rs (L256-261)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;
```

**File:** util/dao/src/lib.rs (L330-332)
```rust
        maximum_withdraws
            .safe_sub(input_capacities)
            .map_err(Into::into)
```

**File:** tx-pool/src/util.rs (L34-41)
```rust
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
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
