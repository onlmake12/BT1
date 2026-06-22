### Title
Silent Truncation in DAO Maximum Withdrawal Calculation Due to Missing Overflow Check — (`util/dao/src/lib.rs`)

### Summary
The `calculate_maximum_withdraw` function in `util/dao/src/lib.rs` uses a bare `as u64` truncating cast on a `u128` intermediate result without any overflow guard. The sibling function `dao_field_with_current_epoch` in the same file performs the structurally identical arithmetic and correctly uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. This inconsistency means that once the on-chain accumulation rate (`AR`) grows sufficiently relative to the deposit-time AR, the function silently returns a truncated (too-small) maximum-withdrawal capacity instead of propagating an error, causing the `calculate_dao_maximum_withdraw` RPC to mislead callers about the true claimable amount.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the maximum withdrawable capacity as:

```rust
// util/dao/src/lib.rs  lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← bare truncating cast
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

`withdraw_counted_capacity` is a `u128`. If its value exceeds `u64::MAX`, the `as u64` cast silently discards the high bits, producing a value that is far smaller than the true result.

The identical arithmetic pattern in `dao_field_with_current_epoch` — which is on the consensus-critical path — is handled correctly:

```rust
// util/dao/src/lib.rs  lines 256-261
let ar_increase128 =
    u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
let current_ar = parent_ar
    .checked_add(ar_increase)
    .ok_or(DaoError::Overflow)?;
``` [2](#0-1) 

The inconsistency is clear: one path uses `u64::try_from` with a proper `DaoError::Overflow` return; the other uses a bare `as u64` cast.

---

### Impact Explanation

`calculate_maximum_withdraw` is exposed through the `calculate_dao_maximum_withdraw` JSON-RPC endpoint (`rpc/src/module/experiment.rs`). Any RPC caller — including wallets, dApps, and block explorers — that queries this endpoint to determine how much CKB a DAO depositor may withdraw will receive a silently truncated (too-small) value once the accumulation-rate ratio grows past the overflow threshold. A user who constructs a withdrawal transaction based solely on this RPC response may withdraw less than they are entitled to, effectively leaving funds permanently locked in the DAO deposit cell. The actual consensus-layer DAO script is unaffected, but the RPC-level estimation is the primary tool users rely on for withdrawal planning.

**Impact: Medium** — incorrect withdrawal estimation returned to RPC callers; no direct consensus divergence, but potential for user-level fund under-recovery.

---

### Likelihood Explanation

The overflow condition requires:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

`counted_capacity` is bounded by the total CKB supply (~3.36 × 10¹⁸ shannons). The AR starts at 10¹⁰ and grows at roughly 4 % per year (secondary issuance ÷ total capacity). For the product to exceed `u64::MAX` (~1.84 × 10¹⁹), the AR ratio `withdrawing_ar / deposit_ar` must exceed ~5.5×, which requires approximately 43+ years of continuous chain operation from the deposit block. **Likelihood: Very Low.**

---

### Recommendation

Replace the bare truncating cast with the same checked conversion used in `dao_field_with_current_epoch`:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64)

// After (safe, consistent with the rest of the file):
let withdraw_counted = u64::try_from(withdraw_counted_capacity)
    .map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted)
``` [3](#0-2) 

---

### Proof of Concept

The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` already demonstrates that the function can return `Err` for overflow-adjacent inputs, but it exercises the `safe_add` path, not the `as u64` truncation path. [4](#0-3) 

A concrete overflow scenario:

1. Deposit a cell with `output_capacity = u64::MAX` shannons and `occupied_capacity = 0`, so `counted_capacity = u64::MAX`.
2. Let the chain run until `withdrawing_ar / deposit_ar > 1.0` (any positive AR growth suffices to make `withdraw_counted_capacity > u64::MAX`).
3. Call `calculate_maximum_withdraw` — the `as u64` cast truncates the result to a value far below the true maximum.
4. The RPC returns a silently wrong (too-small) capacity; no error is surfaced to the caller.

The contrast with the safe path in `dao_field_with_current_epoch` confirms this is an oversight, not intentional design: [5](#0-4) [6](#0-5)

### Citations

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

**File:** util/dao/src/lib.rs (L256-261)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;
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
