### Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Produces Wrong DAO Withdrawal Capacity — (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the maximum withdrawable capacity for a NervosDAO cell using a u128 intermediate value, then narrows it back to u64 with a bare `as u64` cast. In Rust, `as` casts on integers always truncate silently — they never panic or return an error. Every other analogous u128→u64 narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, which propagates a proper error. The inconsistency means that when the intermediate product overflows u64, the function silently returns a wrong (too-small) capacity instead of an error, corrupting the fee calculation and the RPC result for DAO withdrawals.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
// line 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast on line 156 silently discards the upper 64 bits of `withdraw_counted_capacity` whenever it exceeds `u64::MAX`. Rust's `as` cast is defined to wrap/truncate, not to panic or return `Err`.

Every other u128→u64 narrowing in the same file uses the safe form:

```rust
// line 204 — secondary_block_reward
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// line 245 — dao_field_with_current_epoch (miner_issuance)
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)

// line 258 — dao_field_with_current_epoch (ar_increase)
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The overflow condition is:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

`counted_capacity` is at most `u64::MAX` shannons (≈ 184 billion CKB). `withdrawing_ar / deposit_ar` is the interest multiplier (always ≥ 1, since AR is monotonically increasing). The product overflows u64 when the AR has grown enough relative to the deposit AR — concretely, when the ratio exceeds 1.0 by more than `u64::MAX / counted_capacity`.

When the truncated value happens to be small enough that `truncated + occupied_capacity` does not itself overflow u64, `safe_add` succeeds and the function returns a silently wrong (too-small) capacity with no error. The existing test `check_withdraw_calculation_overflows` only exercises the case where the *final* `safe_add` overflows, not the silent truncation path. [5](#0-4) 

---

### Impact Explanation

`calculate_maximum_withdraw` is called from two places:

1. **`transaction_fee`** (via `transaction_maximum_withdraw`), which is called during tx-pool admission in `check_tx_fee`:

```rust
// tx-pool/src/util.rs line 34-41
let fee = DaoCalculator::new(...)
    .transaction_fee(rtx)
    .map_err(|err| Reject::Malformed(...))?;
``` [6](#0-5) 

If `calculate_maximum_withdraw` returns a silently truncated (too-small) value, `transaction_fee` computes `max_withdraw - outputs_capacity`. A too-small `max_withdraw` can make this subtraction underflow, causing `safe_sub` to return `DaoError::Overflow`, which is mapped to `Reject::Malformed`. A **valid DAO withdrawal transaction is permanently rejected** from the tx-pool.

2. **`calculate_dao_maximum_withdraw` RPC** (in `rpc/src/module/experiment.rs`):

```rust
// lines 259-267
match calculator.calculate_maximum_withdraw(...) {
    Ok(capacity) => Ok(capacity.into()),
    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
}
``` [7](#0-6) 

If the truncation does not trigger the downstream `safe_add` overflow, the RPC silently returns a wrong (too-small) withdrawal amount to the caller, causing the user to construct a transaction with an incorrect output capacity that the on-chain DAO script will reject.

---

### Likelihood Explanation

The overflow requires `counted_capacity × withdrawing_ar > u64::MAX × deposit_ar`. Since `counted_capacity ≤ u64::MAX`, this simplifies to `withdrawing_ar / deposit_ar > 1` by a factor proportional to `1 / counted_capacity`. For a cell holding close to the maximum capacity (≈ `u64::MAX` shannons), even a tiny AR growth ratio can trigger the overflow. For smaller cells, a larger AR growth is needed. The AR grows slowly under normal network conditions, making this unlikely in the short term but plausible over a long enough time horizon for large deposits. Any RPC caller or transaction sender submitting a DAO withdrawal is the entry point.

---

### Recommendation

Replace the bare `as u64` cast with the same checked narrowing used everywhere else in the file:

```rust
// Before (line 155-156):
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After:
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
``` [8](#0-7) 

---

### Proof of Concept

Suppose:
- `deposit_ar = 10_000_000_000_000_000` (initial AR, 10^16)
- `withdrawing_ar = 20_000_000_000_000_001` (AR has doubled — possible after many years)
- `counted_capacity = u64::MAX - occupied_capacity ≈ 18_446_744_073_709_551_615`

Then:

```
withdraw_counted_capacity
  = (u64::MAX) * 20_000_000_000_000_001 / 10_000_000_000_000_000
  ≈ 2 * u64::MAX + 1
  > u64::MAX
```

`withdraw_counted_capacity as u64` truncates to `1` (the low 64 bits of `2 * u64::MAX + 1`).

`Capacity::shannons(1).safe_add(occupied_capacity)` succeeds (no overflow), and `calculate_maximum_withdraw` returns `Ok(Capacity::shannons(1 + occupied_capacity))` — a value billions of times smaller than the correct withdrawal amount — with no error. The caller (`transaction_fee`) then computes `fee = (1 + occupied_capacity) - outputs_capacity`, which underflows and causes the valid withdrawal transaction to be rejected as malformed. [9](#0-8)

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

**File:** util/dao/src/lib.rs (L202-205)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
        Ok(Capacity::shannons(reward))
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
