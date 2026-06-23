### Title
Silent u128→u64 Truncation in NervosDAO Withdrawal Capacity Calculation - (`File: util/dao/src/lib.rs`)

### Summary
In `DaoCalculator::calculate_maximum_withdraw`, the intermediate `u128` result of the withdrawal capacity formula is cast to `u64` with a bare `as u64` truncation instead of a checked conversion. When the product `counted_capacity * withdrawing_ar / deposit_ar` exceeds `u64::MAX`, the high bits are silently discarded, producing a wrong (too-small) maximum withdrawal value. Every other analogous u128→u64 conversion in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, making this omission a clear inconsistency with a concrete impact.

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the interest-bearing withdrawal amount as:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast silently truncates any bits above bit 63. If `withdraw_counted_capacity > u64::MAX`, the returned capacity is wrong and smaller than the true value.

Every other u128→u64 narrowing in the same file is guarded:

```rust
// line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
// line 245
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)
// line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The `calculate_maximum_withdraw` function is called from:
1. `transaction_maximum_withdraw` → `transaction_fee` and `withdrawed_interests` (used during block assembly and transaction verification)
2. The `calculate_dao_maximum_withdraw` RPC endpoint, directly reachable by any RPC caller [5](#0-4) [6](#0-5) 

### Impact Explanation

When `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`, the node computes a truncated (too-small) maximum withdrawal amount. The node then uses this wrong value in transaction fee verification (`transaction_fee` calls `maximum_withdraw.safe_sub(outputs_capacity)`). A user attempting to withdraw the correct, larger amount will have their withdrawal transaction rejected by the node because the node believes the output capacity exceeds the (incorrectly computed) maximum. The user's deposited CKB is frozen — they cannot withdraw the correct amount, and any attempt to claim the true interest-bearing balance is rejected.

Additionally, `withdrawed_interests` uses the same path during DAO field computation for block assembly, meaning a block containing such a withdrawal would have an incorrect DAO field, causing consensus divergence.

### Likelihood Explanation

The condition requires `counted_capacity * withdrawing_ar > u64::MAX * deposit_ar`. Since `withdrawing_ar >= deposit_ar` (AR only increases), this simplifies to needing `counted_capacity * (withdrawing_ar / deposit_ar) > u64::MAX`. On a long-running chain where AR has grown by even a small factor, a depositor with a large cell (e.g., near the u64 capacity limit of ~184 billion CKB) can trigger this. The genesis AR is `10^16`; as the chain ages and secondary issuance accumulates, AR grows. A cell with `counted_capacity` near `u64::MAX / 2` (~9.2e18 shannons, ~92 billion CKB) and an AR ratio of just 2× would overflow. While 92 billion CKB is large, the protocol imposes no per-cell cap, and institutional or protocol-level deposits could reach this range. The RPC endpoint `calculate_dao_maximum_withdraw` is also directly callable by any unprivileged user, exposing the wrong value to wallets and causing incorrect user-facing balance displays.

### Recommendation

Replace the silent cast with a checked conversion, consistent with all other u128→u64 conversions in the same file:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    )
    .safe_add(occupied_capacity)?;
```

### Proof of Concept

```
counted_capacity  = 18_446_744_073_709_550_000  (near u64::MAX, ~184 billion CKB)
deposit_ar        = 10_000_000_000_000_000       (genesis AR = 10^16)
withdrawing_ar    = 20_000_000_000_000_000       (AR doubled after long deposit)

withdraw_counted_capacity (u128) = 18_446_744_073_709_550_000 * 20_000_000_000_000_000
                                   / 10_000_000_000_000_000
                                 = 36_893_488_147_419_100_000   ← exceeds u64::MAX (18_446_744_073_709_551_615)

withdraw_counted_capacity as u64 = 36_893_488_147_419_100_000 - 2^64
                                 = 18_446_744_073_709_548_384   ← silently truncated, wrong value

True maximum withdrawal = 36_893_488_147_419_100_000 shannons
Node-computed maximum   = 18_446_744_073_709_548_384 shannons  (≈ half the correct value)

A withdrawal transaction claiming the correct amount is rejected.
User's NervosDAO deposit is frozen.
```

The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` (line 296) uses a capacity near `u64::MAX` with a small AR ratio and asserts `result.is_err()`. However, with the current `as u64` cast, the error only propagates if the truncated value + `occupied_capacity` happens to overflow `safe_add` — it does not reliably catch the silent truncation in all cases where the true result exceeds `u64::MAX`. [7](#0-6) [8](#0-7)

### Citations

**File:** util/dao/src/lib.rs (L108-113)
```rust
                            self.calculate_maximum_withdraw(
                                output,
                                Capacity::bytes(cell_meta.data_bytes as usize)?,
                                deposit_header_hash,
                                withdrawing_header_hash,
                            )
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
