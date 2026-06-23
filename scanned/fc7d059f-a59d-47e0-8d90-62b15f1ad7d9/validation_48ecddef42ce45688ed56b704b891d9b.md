### Title
Silent `u128`→`u64` Truncation in `calculate_maximum_withdraw` Produces Wrong Withdrawal Amount Instead of Error - (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the DAO withdrawal capacity in `u128` to avoid intermediate overflow, but then casts the result back to `u64` with a bare `as u64` — a silent, lossy truncation. Every other analogous `u128`→`u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. The inconsistency means that when the final `u128` result exceeds `u64::MAX`, the function silently returns a drastically wrong (bit-truncated) withdrawal capacity rather than propagating an error, causing a DAO depositor to receive far less than their entitled funds.

---

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

The `as u64` cast is a Rust wrapping/truncating cast: if `withdraw_counted_capacity` (a `u128`) exceeds `u64::MAX`, the high bits are silently discarded and the resulting `Capacity` is wrong. No error is returned.

Contrast this with every other `u128`→`u64` narrowing in the same file, which all use the checked form:

```rust
// line 245
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
// line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) 

The `calculate_maximum_withdraw` function is called both from `transaction_maximum_withdraw` (used during consensus-level DAO withdrawal verification) and from the `calculate_dao_maximum_withdraw` RPC endpoint. [4](#0-3) 

---

### Impact Explanation

When `withdraw_counted_capacity` (u128) exceeds `u64::MAX`, `as u64` silently wraps. The subsequent `safe_add(occupied_capacity)` only catches overflow if the *already-truncated* value plus `occupied_capacity` itself overflows `u64`. In the common case where the truncated value is small (e.g., the high bits wrap to a small number), `safe_add` succeeds and the function returns a `Capacity` that is orders of magnitude smaller than the depositor's actual entitlement.

Concretely: a DAO depositor with a very large cell capacity (near or above `u64::MAX` shannons, which is ~184 billion CKB) would receive a silently wrong, truncated withdrawal amount. The consensus verifier (`CapacityVerifier`) would accept the transaction because the output capacity matches what `calculate_maximum_withdraw` returned — the wrong value — so the depositor permanently loses the difference.

The existing test `check_withdraw_calculation_overflows` asserts `result.is_err()` for a near-`u64::MAX` capacity, but this test passes only because in that specific case the truncated value plus `occupied_capacity` happens to overflow `u64` and `safe_add` catches it. For other overflow magnitudes where the truncated value is small, `safe_add` succeeds silently with a wrong result. [5](#0-4) 

---

### Likelihood Explanation

The NervosDAO is a core, actively used protocol feature. Any user who deposits a sufficiently large amount of CKB into the DAO and later withdraws is an unprivileged transaction sender who can trigger this path. The `calculate_dao_maximum_withdraw` RPC is also publicly callable. While the threshold (near `u64::MAX` shannons ≈ 184 billion CKB) is high, the silent nature of the failure — returning a wrong value rather than an error — makes it particularly dangerous because neither the user nor the node detects the corruption.

---

### Recommendation

Replace the silent `as u64` cast with the checked conversion already used elsewhere in the same file:

```rust
// Before (line 156):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After:
let withdraw_counted_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_u64).safe_add(occupied_capacity)?
```

This is consistent with the pattern at lines 245 and 258 and ensures that an overflow is surfaced as a `DaoError::Overflow` rather than silently producing a wrong withdrawal amount.

---

### Proof of Concept

The root cause is the single `as u64` cast at line 156 of `util/dao/src/lib.rs`: [1](#0-0) 

**Scenario:**

1. A depositor creates a DAO cell with capacity near `u64::MAX` shannons (e.g., `18_446_744_073_709_550_000`).
2. After some epochs, `withdrawing_ar > deposit_ar`, so `withdraw_counted_capacity` (u128) = `counted_capacity * withdrawing_ar / deposit_ar` exceeds `u64::MAX`.
3. `withdraw_counted_capacity as u64` silently truncates to a small value (e.g., `49_616` shannons in the test case).
4. `Capacity::shannons(49_616).safe_add(occupied_capacity)` succeeds — no error is returned.
5. The function returns a `Capacity` of ~`49_616 + occupied_capacity` shannons instead of the correct ~`18_446_744_073_709_550_000+` shannons.
6. The depositor's withdrawal transaction is built using this wrong amount and accepted by consensus, permanently losing the difference.

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

**File:** util/dao/src/lib.rs (L244-245)
```rust
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/dao/src/lib.rs (L258-258)
```rust
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/src/tests.rs (L296-349)
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
```
