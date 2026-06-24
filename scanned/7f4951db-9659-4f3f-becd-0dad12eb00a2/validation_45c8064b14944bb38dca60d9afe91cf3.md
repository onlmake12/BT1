Audit Report

## Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Returns Wrong DAO Withdrawal Capacity — (`util/dao/src/lib.rs`)

## Summary
`DaoCalculator::calculate_maximum_withdraw` narrows a u128 intermediate product back to u64 with a bare `as u64` cast at line 156, which silently truncates rather than propagating an error. Every other u128→u64 narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. When the intermediate product exceeds `u64::MAX` and the truncated remainder plus `occupied_capacity` does not itself overflow, the function returns `Ok` with a drastically wrong (too-small) capacity, corrupting both the tx-pool fee check and the RPC withdrawal estimate for NervosDAO cells.

## Finding Description
In `util/dao/src/lib.rs` lines 152–156, `calculate_maximum_withdraw` computes:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← bare truncating cast
        .safe_add(occupied_capacity)?;
```

The `as u64` cast discards the upper 64 bits whenever `withdraw_counted_capacity > u64::MAX`. Rust's `as` cast is defined to wrap/truncate with no panic or `Err`.

The three other u128→u64 narrowings in the same file all use the safe form:
- Line 204: `u64::try_from(reward128).map_err(|_| DaoError::Overflow)?`
- Line 245: `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?`
- Line 258: `u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?`

The dangerous silent path is: `withdraw_counted_capacity > u64::MAX` **and** `(withdraw_counted_capacity mod 2^64) + occupied_capacity ≤ u64::MAX`. In that case `safe_add` succeeds and the function returns `Ok(wrong_capacity)` with no indication of error.

The existing test `check_withdraw_calculation_overflows` (lines 295–350 of `util/dao/src/tests.rs`) uses an AR ratio of approximately `1 + 10^{-10}`, which pushes `withdraw_counted_capacity` only slightly above `u64::MAX`. The truncated remainder is large enough that the subsequent `safe_add(occupied_capacity)` itself overflows and returns `Err` — so the test passes for the wrong reason and does not cover the silent-truncation path at all.

## Impact Explanation
Two callers are affected:

1. **`transaction_fee` → tx-pool admission** (`tx-pool/src/util.rs` lines 34–41): `transaction_fee` computes `max_withdraw - outputs_capacity`. A silently truncated (too-small) `max_withdraw` makes `safe_sub` underflow, causing `Reject::Malformed`. A valid DAO withdrawal transaction is permanently rejected by the local node's tx-pool.

2. **`calculate_dao_maximum_withdraw` RPC** (`rpc/src/module/experiment.rs` lines 259–267): The RPC silently returns a wrong (billions-of-times-too-small) withdrawal capacity to the caller. The caller constructs a transaction with an incorrect output capacity that the on-chain DAO script rejects.

Both outcomes directly impair the NervosDAO withdrawal mechanism, which is a core economic function of CKB. This matches the allowed impact: **Vulnerabilities which could easily damage CKB economy**.

## Likelihood Explanation
The overflow condition is `counted_capacity × withdrawing_ar > u64::MAX × deposit_ar`. Since `counted_capacity ≤ total_CKB_supply ≈ 3.36 × 10^{18}` shannons and `u64::MAX ≈ 1.84 × 10^{19}`, the AR must grow by a factor of roughly **5.5×** from its initial value of `10^{16}` for a cell holding the entire genesis supply. For smaller cells the required growth factor is proportionally larger. Under normal network conditions this takes many years, making the vulnerability a long-term rather than immediately exploitable risk. No attacker action is required beyond depositing into the DAO and waiting; the trigger is purely a function of elapsed time and deposit size. Any RPC caller or transaction submitter is the entry point once the condition is met.

## Recommendation
Replace the bare `as u64` cast with the same checked narrowing used everywhere else in the file:

```rust
// Before (lines 155–156):
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After:
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
```

Additionally, add a unit test that sets `withdrawing_ar ≈ 2 × deposit_ar` with a large `counted_capacity` and asserts `result == Err(DaoError::Overflow)`, covering the silent-truncation path that `check_withdraw_calculation_overflows` currently misses.

## Proof of Concept
Construct headers with:
- `deposit_ar = 10_000_000_000_000_000`
- `withdrawing_ar = 20_000_000_000_000_001` (AR doubled)
- Cell `capacity = u64::MAX` shannons, no output data, `occupied_capacity = 0`

Then:
```
withdraw_counted_capacity
  = u64::MAX * 20_000_000_000_000_001 / 10_000_000_000_000_000
  ≈ 2 × u64::MAX + 1
```

`withdraw_counted_capacity as u64` truncates to `1`.

`Capacity::shannons(1).safe_add(Capacity::zero())` succeeds.

`calculate_maximum_withdraw` returns `Ok(Capacity::shannons(1))` — a value ~18 quintillion times smaller than correct — with no error. `transaction_fee` then computes `1 - outputs_capacity`, which underflows, and the valid withdrawal transaction is rejected as `Reject::Malformed`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
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
