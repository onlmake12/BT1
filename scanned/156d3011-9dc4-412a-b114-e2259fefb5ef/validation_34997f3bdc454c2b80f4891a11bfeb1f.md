### Title
Silent `u128`→`u64` Truncation in `calculate_maximum_withdraw` Produces Incorrect DAO Withdrawal Capacity — (File: `util/dao/src/lib.rs`)

---

### Summary

In `calculate_maximum_withdraw`, the intermediate `withdraw_counted_capacity` value is computed as a `u128` to avoid overflow during multiplication, but is then silently narrowed back to `u64` via an infallible `as u64` cast. Every other `u128`→`u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. The inconsistency means that when `withdraw_counted_capacity` exceeds `u64::MAX`, the high bits are silently discarded and the function returns a drastically smaller — but error-free — withdrawal capacity instead of propagating an overflow error.

---

### Finding Description

`calculate_maximum_withdraw` in `util/dao/src/lib.rs` computes the maximum CKB a depositor may withdraw from NervosDAO:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncating cast
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast in Rust is a wrapping/truncating operation: if `withdraw_counted_capacity > u64::MAX`, the value wraps modulo `2^64` with no panic and no error. The result fed into `Capacity::shannons(...)` is then a fraction of the correct value.

Contrast this with every other `u128`→`u64` narrowing in the same file, which uses checked conversion:

```rust
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
``` [2](#0-1) 

```rust
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [3](#0-2) 

The `ar` accumulation rate is a `u64` stored in the DAO field of every block header, starting at `10_000_000_000_000_000` (10^16) and growing each block by `parent_ar * g2 / parent_c`: [4](#0-3) [5](#0-4) 

The overflow condition for the silent truncation is:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

and simultaneously:

```
(withdraw_counted_capacity mod 2^64) + occupied_capacity  ≤  u64::MAX
```

When both conditions hold, `safe_add` does not catch the error, and the function silently returns a far-too-small withdrawal capacity.

The existing test `check_withdraw_calculation_overflows` uses an unrealistic capacity (`18_446_744_073_709_550_000` shannons ≈ 184 billion CKB) and asserts `result.is_err()`. In that specific test the overflow is caught by `safe_add` — not by the `as u64` cast — because the truncated value plus `occupied_capacity` still overflows `u64`. The silent-truncation window (where `safe_add` does not catch it) is not covered by any test. [6](#0-5) 

---

### Impact Explanation

`calculate_maximum_withdraw` is called during DAO withdrawal transaction validation via `transaction_maximum_withdraw` → `transaction_fee`. If the function silently returns a value far below the true maximum:

1. **Incorrect fee accounting**: `transaction_fee` subtracts outputs capacity from the (truncated) maximum withdraw, producing a wildly incorrect fee that can cause the transaction to be rejected from the tx-pool or accepted with a fabricated fee.
2. **Depositor fund loss / lock-up**: A user who constructs a withdrawal claiming the correct (larger) amount would have their transaction rejected by the node's verifier, even though the on-chain DAO type script would accept it. The depositor cannot reclaim their full principal plus interest.

The impact class is **cell/capacity accounting** — the same class as the reference report.

---

### Likelihood Explanation

**Low.** The total CKB issuance is approximately 33.6 billion CKB ≈ 3.36 × 10^18 shannons, well below `u64::MAX` (≈ 1.84 × 10^19 shannons). For `withdraw_counted_capacity` to exceed `u64::MAX`, the ratio `withdrawing_ar / deposit_ar` must exceed `u64::MAX / counted_capacity`. With `counted_capacity` bounded by the total supply, the ratio must exceed ≈ 5.5×, which corresponds to roughly 43 years of secondary issuance accumulation at the current rate (~4 % annual `ar` growth). No single cell can hold more than the total supply, so the condition cannot be triggered today. The risk grows over the multi-decade lifetime of the chain.

---

### Recommendation

Replace the silent `as u64` cast with the same checked pattern used elsewhere in the file:

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

This makes the overflow path explicit and consistent with `miner_issuance128` and `ar_increase128` handling in `dao_field_with_current_epoch`.

---

### Proof of Concept

The silent-truncation window can be demonstrated with synthetic `ar` values (not requiring real chain state):

```rust
// deposit_ar and withdrawing_ar chosen so that:
//   counted_capacity * withdrawing_ar / deposit_ar  ==  u64::MAX + 1_000_000
// but
//   (u64::MAX + 1_000_000) mod 2^64  +  occupied_capacity  <=  u64::MAX
//
// => safe_add does NOT overflow; function returns Ok(Capacity::shannons(~1_000_000 + occupied))
// instead of the correct ~u64::MAX shannons.

let deposit_ar:     u64 = 10_000_000_000_000_000;
let withdrawing_ar: u64 = 10_000_000_000_000_001; // tiny increase

// counted_capacity chosen so product just crosses u64::MAX
let counted_capacity: u64 = u64::MAX; // hypothetical; exceeds real supply

let result = u128::from(counted_capacity)
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);

// result > u64::MAX; `as u64` silently wraps
let truncated = result as u64;
// truncated is a tiny value; safe_add(occupied_capacity) succeeds
// => calculate_maximum_withdraw returns Ok with a drastically wrong capacity
```

The root cause is the single `as u64` cast at: [7](#0-6)

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

**File:** util/dao/utils/src/lib.rs (L104-110)
```rust
pub fn extract_dao_data(dao: Byte32) -> (u64, Capacity, Capacity, Capacity) {
    let data = dao.raw_data();
    let c = Capacity::shannons(LittleEndian::read_u64(&data[0..8]));
    let ar = LittleEndian::read_u64(&data[8..16]);
    let s = Capacity::shannons(LittleEndian::read_u64(&data[16..24]));
    let u = Capacity::shannons(LittleEndian::read_u64(&data[24..32]));
    (ar, c, s, u)
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
