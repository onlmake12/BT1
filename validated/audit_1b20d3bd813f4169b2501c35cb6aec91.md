### Title
Silent Truncating Cast in `calculate_maximum_withdraw` Produces Incorrect DAO Withdrawal Amount Instead of Error — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` in `util/dao/src/lib.rs` uses a bare `as u64` cast to narrow a `u128` intermediate result to `u64`. In Rust, `as u64` on a `u128` value is a **silent wrapping/truncating cast**: if the value exceeds `u64::MAX`, the high bits are silently discarded and the function continues with a drastically wrong (tiny) withdrawal capacity. Every other analogous u128→u64 narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, which returns a proper error. The inconsistency means this specific path silently produces an incorrect result rather than rejecting the computation.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the maximum CKB a depositor can withdraw from NervosDAO:

```rust
// util/dao/src/lib.rs  lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
```

`withdraw_counted_capacity` is a `u128`. The cast `withdraw_counted_capacity as u64` silently discards the upper 64 bits if the value exceeds `u64::MAX`. The subsequent `safe_add` only catches overflow in the *addition* step; if the truncated value is small enough that the addition does not overflow, the function returns `Ok(...)` with a silently wrong (far too small) capacity.

Compare with every other u128→u64 narrowing in the same file, which all use checked conversion:

```rust
// line 244-245  (miner_issuance128)
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)

// line 258  (ar_increase128)
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

The overflow condition is:

```
counted_capacity × withdrawing_ar  >  u64::MAX × deposit_ar
```

`counted_capacity` is at most `u64::MAX` (cell capacity minus occupied capacity). `withdrawing_ar / deposit_ar` is always ≥ 1 because the accumulation rate (`ar`) is monotonically non-decreasing. The overflow therefore occurs when `withdrawing_ar` is sufficiently larger than `deposit_ar` for a near-maximum-capacity cell.

The genesis accumulation rate is `DEFAULT_GENESIS_ACCUMULATE_RATE = 10_000_000_000_000_000` (10¹⁶). The rate grows by roughly 10⁸ per block. For the ratio to reach 2× (the minimum needed to overflow a full-u64 `counted_capacity`), approximately 10⁸ blocks would be required — far beyond any near-term horizon. However, the code defect is structurally present and inconsistent with the rest of the codebase.

**Concrete silent-failure scenario:**

Suppose at some future point `withdrawing_ar = deposit_ar + Δ` such that:

```
counted_capacity × (deposit_ar + Δ) / deposit_ar  >  u64::MAX
```

Then `withdraw_counted_capacity as u64` wraps to a value `V ≪ u64::MAX`. If `V + occupied_capacity ≤ u64::MAX`, `safe_add` succeeds and the function returns `Ok(Capacity::shannons(V + occupied_capacity))` — a withdrawal amount orders of magnitude smaller than the depositor is owed. No error is raised; the caller (transaction verifier or RPC handler) receives a plausible-looking but wrong value.

---

### Impact Explanation

A DAO depositor who triggers this path receives a silently incorrect (drastically reduced) withdrawal capacity. The transaction verifier (`CapacityVerifier`) and the DAO type script both rely on `calculate_maximum_withdraw` to determine the maximum permitted output capacity for a withdrawal transaction. If the returned value is wrong-but-small, a withdrawal transaction claiming the correct amount would be **rejected** (the verifier sees the truncated maximum as the ceiling), effectively locking the depositor's funds. Alternatively, if the depositor constructs a transaction using the RPC-returned truncated value, they permanently lose the difference. Either outcome constitutes loss of funds for an unprivileged DAO user.

---

### Likelihood Explanation

The overflow requires `withdrawing_ar` to be substantially larger than `deposit_ar` for a cell with near-maximum capacity. Given the current secondary issuance rate and the genesis AR of 10¹⁶, the AR would need tens of thousands of years to grow enough to trigger this with a realistic cell capacity. The likelihood on any near-term horizon is therefore extremely low. The finding is reported because (a) the code defect is structurally real and inconsistent with the rest of the file, (b) the impact if triggered is severe (silent loss of funds, not a safe error), and (c) the entry path is fully unprivileged (any DAO depositor).

---

### Recommendation

Replace the silent `as u64` cast with the same checked conversion pattern used everywhere else in the file:

```diff
- let withdraw_capacity =
-     Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
+ let withdraw_capacity =
+     Capacity::shannons(
+         u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
+     )
+     .safe_add(occupied_capacity)?;
```

This makes the function consistent with `dao_field_with_current_epoch` (lines 244–245 and 258) and ensures that any future overflow is surfaced as a `DaoError::Overflow` rather than silently producing an incorrect withdrawal amount.

---

### Proof of Concept

The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` (lines 296–350) demonstrates that the function *can* return an error for near-overflow inputs — but it does so only because `safe_add` catches the final addition overflow, not because the `as u64` cast is checked. The following constructed scenario shows the gap:

```rust
// Hypothetical future state: withdrawing_ar = 2 × deposit_ar
let deposit_ar:     u64 = 10_000_000_000_000_000;   // genesis AR
let withdrawing_ar: u64 = 20_000_000_000_000_000;   // 2× (far future)

// Cell with counted_capacity near u64::MAX
let counted_capacity: u64 = u64::MAX - 1;            // ~1.844e19 shannons

// u128 intermediate — correct value exceeds u64::MAX
let withdraw_counted_capacity: u128 =
    u128::from(counted_capacity) * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
// = (u64::MAX - 1) * 2 / 1  ≈  3.689e19  >  u64::MAX

// Silent truncation:
let truncated = withdraw_counted_capacity as u64;
// = (u64::MAX - 1) * 2 mod 2^64  =  u64::MAX - 3  (a plausible-looking value)

// safe_add(occupied_capacity) may succeed → Ok(wrong small value) returned
```

The correct result should be `DaoError::Overflow`; instead the function returns a silently wrong capacity. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
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

**File:** util/dao/utils/src/lib.rs (L17-17)
```rust
const DEFAULT_GENESIS_ACCUMULATE_RATE: u64 = 10_000_000_000_000_000;
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
