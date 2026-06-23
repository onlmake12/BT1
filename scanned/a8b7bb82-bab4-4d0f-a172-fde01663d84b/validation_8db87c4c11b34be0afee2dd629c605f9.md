### Title
Unchecked `u128 as u64` Truncation in DAO Withdrawal Capacity Calculation — (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes a u128 intermediate value and narrows it to u64 with an unchecked `as u64` cast. If the intermediate value exceeds `u64::MAX`, the cast silently truncates, producing a far smaller withdrawal capacity than the depositor is entitled to. Every other u128→u64 narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, making this omission a clear inconsistency with the established pattern.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the DAO interest-adjusted withdrawal amount as:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← unchecked cast
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast is a silent truncation: if `withdraw_counted_capacity > u64::MAX`, the high bits are discarded and the result wraps to a small value with no error returned.

Every other u128→u64 narrowing in the same file is guarded:

```rust
// secondary_block_reward
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// dao_field_with_current_epoch (miner_issuance)
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)

// dao_field_with_current_epoch (ar_increase)
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The overflow condition is:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

`counted_capacity` is bounded by total CKB supply (~3.36 × 10¹⁸ shannons). `ar` starts at `10_000_000_000` and grows monotonically. The ratio `withdrawing_ar / deposit_ar` must exceed ~5.5× for a maximum-capacity cell to trigger the overflow. This is a long-horizon scenario, but the code is structurally incorrect today.

---

### Impact Explanation

When the truncation fires, `withdraw_counted_capacity as u64` wraps to a value far below the true entitlement. Two concrete consequences follow:

1. **Silent under-payment**: `withdraw_capacity` is computed as a tiny value. `transaction_fee = maximum_withdraw − outputs_capacity` underflows inside `safe_sub`, causing the withdrawal transaction to be rejected with `DaoError` even though it is economically valid. The depositor cannot reclaim their funds through the normal path.

2. **Consensus divergence risk**: If nodes running different software versions (one with the bug, one patched) disagree on whether a DAO withdrawal transaction is valid, they may accept or reject different blocks, causing a chain split.

The function is called during transaction verification: [5](#0-4) 

and is also directly exposed via the RPC experiment module: [6](#0-5) 

---

### Likelihood Explanation

The overflow requires the `ar` accumulation rate to grow to more than ~5.5× its genesis value while a single cell holds near-maximum capacity. Under current CKB secondary issuance parameters this is a long-term scenario. However:

- The bug is reachable by any unprivileged transaction sender submitting a DAO withdrawal.
- No special permissions, keys, or majority hashpower are required.
- The code inconsistency (three guarded casts vs. one unguarded cast in the same function file) is a latent defect that will eventually become exploitable as `ar` grows, and is already a correctness hazard for any future parameter changes.

---

### Recommendation

Replace the unchecked cast with the same checked pattern used everywhere else in the file:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (safe, consistent with the rest of the file):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?
``` [7](#0-6) 

---

### Proof of Concept

The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` already probes near-overflow territory but relies on `safe_add` to catch the error after the truncation — it does not verify that the truncation itself is caught. [8](#0-7) 

A direct demonstration of the silent truncation:

```rust
// Simulate: counted_capacity = u64::MAX, withdrawing_ar = 2 * deposit_ar
let counted_capacity: u64 = u64::MAX;
let deposit_ar: u64 = 10_000_000_000;
let withdrawing_ar: u64 = 20_000_000_000; // 2× deposit_ar

let withdraw_counted_capacity: u128 =
    u128::from(counted_capacity) * u128::from(withdrawing_ar) / u128::from(deposit_ar);
// withdraw_counted_capacity = 2 * u64::MAX = 36_893_488_147_419_103_230 > u64::MAX

let truncated = withdraw_counted_capacity as u64;
// truncated = 36_893_488_147_419_103_230 % 2^64 = u64::MAX - 1
// Silent wrap: result is u64::MAX - 1 instead of 2 * u64::MAX
// No error is returned; the caller receives a silently wrong value.

// With the fix:
let checked = u64::try_from(withdraw_counted_capacity); // Err(_) → DaoError::Overflow
```

This confirms that the `as u64` cast silently wraps rather than propagating `DaoError::Overflow`, contrary to the contract established by every other arithmetic operation in `DaoCalculator`. [1](#0-0)

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

**File:** util/dao/src/lib.rs (L202-204)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
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

**File:** rpc/src/module/experiment.rs (L1-14)
```rust
use crate::error::RPCError;
use crate::module::chain::CyclesEstimator;
use async_trait::async_trait;
use ckb_dao::DaoCalculator;
use ckb_jsonrpc_types::{
    Capacity, DaoWithdrawingCalculationKind, EstimateCycles, EstimateMode, OutPoint, Transaction,
    Uint64,
};
use ckb_shared::{Snapshot, shared::Shared};
use ckb_store::ChainStore;
use ckb_types::{core, packed};
use jsonrpc_core::Result;
use jsonrpc_utils::rpc;

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
