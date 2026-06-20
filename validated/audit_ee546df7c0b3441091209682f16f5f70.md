### Title
Silent Truncating Cast in `calculate_maximum_withdraw` Produces Incorrect DAO Withdrawal Capacity — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes a u128 intermediate value (`withdraw_counted_capacity`) and then casts it to `u64` using the Rust `as u64` operator, which silently truncates on overflow. Every other analogous u128→u64 narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, making this the sole unguarded cast. When the result exceeds `u64::MAX`, the function silently returns a drastically wrong (truncated) capacity instead of propagating an error, corrupting DAO interest accounting for any transaction that exercises this path.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the maximum withdrawable capacity for a NervosDAO cell:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The intermediate product is widened to u128 to avoid overflow during multiplication, but the final narrowing back to u64 is done with `as u64`, which is a **bit-truncating cast** — it discards the upper 64 bits silently and never returns an error.

Every other u128→u64 narrowing in the same file is guarded:

- `secondary_block_reward`: `u64::try_from(reward128).map_err(|_| DaoError::Overflow)?` [2](#0-1) 

- `dao_field_with_current_epoch`: `u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?` [3](#0-2) 

The formula is:

```
withdraw_counted_capacity = counted_capacity × withdrawing_ar / deposit_ar
```

`withdrawing_ar` is the accumulate rate at withdrawal time; `deposit_ar` is the rate at deposit time. Because AR is monotonically non-decreasing, `withdrawing_ar ≥ deposit_ar` always holds, so `withdraw_counted_capacity ≥ counted_capacity`. When `counted_capacity` is close to `u64::MAX` and any interest has accrued (`withdrawing_ar > deposit_ar`), the u128 result exceeds `u64::MAX` and the `as u64` cast wraps it to a small, incorrect value.

---

### Impact Explanation

`calculate_maximum_withdraw` is the authoritative function used to:

1. Verify DAO withdrawal transactions during block validation (called from `transaction_maximum_withdraw`, which feeds into fee and capacity checks). [4](#0-3) 

2. Serve the `calculate_dao_maximum_withdraw` RPC endpoint. [5](#0-4) 

When truncation occurs, the returned capacity is a drastically smaller value than the correct one. This produces two concrete effects:

- **Incorrect DAO interest accounting**: The node computes a wrong (too-small) maximum withdrawal, causing valid DAO withdrawal transactions to be rejected (the output capacity exceeds the truncated maximum), or — if the truncated value happens to be accepted — allowing a withdrawal that pays less than the correct interest.
- **RPC misinformation**: `calculate_dao_maximum_withdraw` returns a wrong value to callers, misleading wallets and users about their withdrawable balance.

Both effects are consensus-critical: nodes that compute the truncated value will disagree with nodes that do not, or with the correct protocol behavior.

---

### Likelihood Explanation

The trigger condition requires a DAO cell with `counted_capacity` (output capacity minus occupied capacity) large enough that `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`. The genesis accumulate rate is `10_000_000_000_000_000` (10^16). For a cell with `counted_capacity ≈ u64::MAX ≈ 1.844 × 10^19` shannons (~184 billion CKB), even a tiny AR growth (e.g., `withdrawing_ar = deposit_ar + 1`) causes overflow. While 184 billion CKB exceeds the current total supply, the threshold scales down as AR grows over time: after sufficient epochs, cells with much smaller capacities become vulnerable. Any unprivileged transaction sender who controls a large DAO deposit and submits a withdrawal transaction reaches this code path.

---

### Recommendation

Replace the silent `as u64` cast with the same checked conversion used elsewhere in the file:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64)

// After (safe, consistent with the rest of the file):
let withdraw_counted_u64 = u64::try_from(withdraw_counted_capacity)
    .map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_u64)
``` [6](#0-5) 

---

### Proof of Concept

The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` already sets up a near-`u64::MAX` capacity cell and expects an error, but the error it catches comes from the subsequent `safe_add(occupied_capacity)` call — not from the truncating cast itself. [7](#0-6) 

A targeted PoC demonstrating silent truncation (no error returned, wrong value):

```rust
// deposit_ar = 10_000_000_000_000_000 (genesis default)
// withdrawing_ar = 10_000_000_001_000_000 (tiny growth after some epochs)
// counted_capacity = u64::MAX - occupied_capacity (e.g., 18_446_744_073_709_551_615 - 6_100_000_000)

let deposit_ar:    u64 = 10_000_000_000_000_000;
let withdrawing_ar: u64 = 10_000_000_001_000_000;
let counted_capacity: u64 = 18_446_744_067_609_551_615; // u64::MAX - ~6.1 CKB

let result_u128 = u128::from(counted_capacity)
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);

// result_u128 ≈ 18_446_744_069_454_226_022  (> u64::MAX = 18_446_744_073_709_551_615)
// result_u128 as u64 ≈ 1_744_674_407  ← drastically wrong, no error raised
assert!(result_u128 > u64::MAX as u128);  // overflows
let truncated = result_u128 as u64;       // silent truncation, wrong value
```

The function returns `Ok(Capacity::shannons(truncated + occupied_capacity))` — a value billions of shannons smaller than the correct withdrawal amount — with no error, no panic, and no indication that anything went wrong.

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

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L204-204)
```rust
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
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
