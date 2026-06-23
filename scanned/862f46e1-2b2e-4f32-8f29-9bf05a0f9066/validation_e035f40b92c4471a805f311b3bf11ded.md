### Title
Silent u128→u64 Truncating Cast in DAO Withdrawal Capacity Calculation - (File: `util/dao/src/lib.rs`)

---

### Summary
`DaoCalculator::calculate_maximum_withdraw` computes the maximum withdrawable capacity using a u128 intermediate value but converts it back to u64 via a silent truncating `as u64` cast instead of a checked conversion. This silently produces a wrong (smaller) capacity value when the intermediate result exceeds `u64::MAX`, unlike every analogous calculation in the same codebase which uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`.

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

The `as u64` cast silently discards the upper 64 bits when `withdraw_counted_capacity > u64::MAX`. This is not caught by `overflow-checks = true` in `[profile.release]`, because that flag only traps arithmetic operators (`+`, `*`, etc.), not explicit type casts. [2](#0-1) 

Every analogous u128→u64 narrowing in the same file uses the safe pattern:

```rust
// dao_field_with_current_epoch, line 244-245
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)

// dao_field_with_current_epoch, line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [3](#0-2) [4](#0-3) 

`calculate_maximum_withdraw` is the only site that uses the unsafe `as u64` cast.

The overflow condition is reachable when:
- `counted_capacity` is large (close to `u64::MAX` shannons, i.e., ~184 billion CKB)
- `withdrawing_ar / deposit_ar > 1` (always true after any interest accrual, since `ar` only grows)

Concretely: `counted_capacity * withdrawing_ar` can exceed `u64::MAX * deposit_ar` when `counted_capacity` is near `u64::MAX` and the ratio `withdrawing_ar / deposit_ar` is even slightly above 1.

---

### Impact Explanation

`calculate_maximum_withdraw` is called from `transaction_maximum_withdraw`, which feeds `transaction_fee` and is used during DAO withdrawal (phase 2) transaction verification. [5](#0-4) 

When the truncation fires, the node computes a **silently wrong, smaller** maximum withdrawal capacity. Two concrete consequences:

1. **Consensus split / incorrect rejection**: The node's off-chain verification rejects a DAO withdrawal transaction that the on-chain DAO type script (running in CKB-VM, which uses the correct formula) would accept. This creates a split between what the node considers valid and what the chain actually enforces.

2. **Incorrect fee accounting**: `transaction_fee = maximum_withdraw - outputs_capacity`. A truncated `maximum_withdraw` causes the fee to be computed as negative (triggering `safe_sub` underflow → `DaoError::Overflow`), causing the node to reject a legitimate withdrawal.

The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` asserts `result.is_err()` for a near-`u64::MAX` capacity, but the current `as u64` code does **not** guarantee an error — it silently truncates and may return `Ok` with a wrong value, meaning the test's expectation is not reliably enforced by the implementation. [6](#0-5) 

---

### Likelihood Explanation

Any unprivileged user who has deposited a very large DAO cell (near `u64::MAX` shannons ≈ 184 billion CKB) and attempts a withdrawal can trigger this. While 184 billion CKB is a large amount, the total CKB supply is ~33.6 billion initially with ongoing issuance, so this is not reachable on mainnet today. However, on testnets or in future supply scenarios, or if an attacker crafts a malicious block with a genesis-level large allocation, this path becomes reachable. The vulnerability class (silent truncation vs. checked conversion) is a latent correctness defect that is one supply-growth away from being exploitable.

---

### Recommendation

Replace the silent `as u64` cast with the same checked pattern used elsewhere in the file:

```rust
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    )
    .safe_add(occupied_capacity)?;
``` [7](#0-6) 

---

### Proof of Concept

Root cause — the inconsistency between the two patterns in the same file:

```
// UNSAFE (calculate_maximum_withdraw, line 156):
Capacity::shannons(withdraw_counted_capacity as u64)
//                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
//                 silently truncates if > u64::MAX

// SAFE (dao_field_with_current_epoch, line 245):
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)
//                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
//                 returns DaoError::Overflow if > u64::MAX
```

Trigger values (approximate):
- `counted_capacity` = `u64::MAX - 1` shannons
- `withdrawing_ar` = `10_000_000_001_000_000` (slightly grown from genesis `10^16`)
- `deposit_ar` = `10_000_000_000_000_000` (genesis value)
- `withdraw_counted_capacity` = `(u64::MAX - 1) * 10_000_000_001_000_000 / 10_000_000_000_000_000` ≈ `u64::MAX * 1.0000000001` → exceeds `u64::MAX` → `as u64` truncates to a small wrong value instead of returning `DaoError::Overflow`. [8](#0-7)

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

**File:** util/dao/src/lib.rs (L244-245)
```rust
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/dao/src/lib.rs (L256-258)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

**File:** Cargo.toml (L318-319)
```text
[profile.release]
overflow-checks = true
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
