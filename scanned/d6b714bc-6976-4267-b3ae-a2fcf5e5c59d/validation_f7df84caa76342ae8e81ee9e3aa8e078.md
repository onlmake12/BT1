### Title
Missing Monotonicity Check on `ar` (Accumulate Rate) in NervosDAO Withdrawal Calculation Allows Silent Loss of Depositor Funds — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` in `util/dao/src/lib.rs` reads `deposit_ar` and `withdrawing_ar` from block headers and computes the withdrawal amount as `counted_capacity * withdrawing_ar / deposit_ar`. The `ar` (accumulate rate) is a monotonically non-decreasing value by protocol design — it can only stay the same or increase over time. However, the function performs **no sanity check** that `withdrawing_ar >= deposit_ar`. If `withdrawing_ar < deposit_ar` (due to a corrupted block header, a crafted block accepted during a reorg, or any bug in the DAO field computation pipeline), the withdrawal silently returns **less capacity than was deposited**, causing the depositor to lose principal with no error.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` does the following:

1. Checks that `deposit_header.number() < withdrawing_header.number()` — a block-number ordering check is present.
2. Reads `deposit_ar` and `withdrawing_ar` from the respective block headers' DAO fields via `extract_dao_data`.
3. Computes `withdraw_counted_capacity = counted_capacity * withdrawing_ar / deposit_ar` using integer arithmetic.
4. Returns `withdraw_counted_capacity + occupied_capacity`.

There is **no check** that `withdrawing_ar >= deposit_ar`.

```
let (deposit_ar, _, _, _) = extract_dao_data(deposit_header.dao());
let (withdrawing_ar, _, _, _) = extract_dao_data(withdrawing_header.dao());
// ...
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
```

If `withdrawing_ar < deposit_ar`, the integer division silently produces a value smaller than `counted_capacity`, meaning the depositor receives **less than their principal**. The function returns `Ok(...)` with no error.

The `ar` field is computed per-block in `dao_field_with_current_epoch`:

```
let ar_increase128 =
    u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
let current_ar = parent_ar
    .checked_add(ar_increase)
    .ok_or(DaoError::Overflow)?;
```

`ar_increase` is computed as `parent_ar * current_g2 / parent_c`. Due to integer truncation, if `current_g2` is very small relative to `parent_c`, `ar_increase` rounds to **zero**, meaning `current_ar == parent_ar`. This is expected and benign in normal operation. However, the protocol provides **no enforcement** that a block's `ar` field is `>= parent_ar` during block verification. A block with a crafted or corrupted DAO field embedding a smaller `ar` than its parent would pass the block-number ordering check in `calculate_maximum_withdraw` and silently produce a reduced withdrawal.

---

### Impact Explanation

A depositor who locks CKB in NervosDAO and later withdraws would receive **less than their deposited principal** — a direct, quantifiable loss of funds. The magnitude of loss is proportional to how much `withdrawing_ar` is below `deposit_ar`. In the extreme case where `withdrawing_ar` is set to 1 and `deposit_ar` is the normal value (~10^16), the depositor would receive essentially zero counted capacity (only `occupied_capacity` is returned). This is a **loss of depositor principal**, not merely loss of interest.

---

### Likelihood Explanation

The `ar` field in a block's DAO data is computed by the node itself during block assembly (`dao_field_with_current_epoch`) and is verified during contextual block verification. Under normal operation, `ar` is monotonically non-decreasing. However:

- A block relayer or miner who crafts a block with a manipulated DAO field (setting `ar` to a lower value) and gets it accepted into the canonical chain would trigger this path.
- A bug in the DAO field computation (e.g., integer overflow in `ar_increase` that wraps, or a future code change) could produce a block with a lower `ar` than its parent.
- The absence of a monotonicity check in `calculate_maximum_withdraw` means the damage is silent — no error is returned, the transaction is accepted, and the depositor loses funds.

The attacker entry path is: submit a crafted block (as a miner or via block relay) with a manipulated DAO `ar` field → the withdrawal transaction referencing that block's header as the withdrawing header will silently compute a reduced payout.

---

### Recommendation

Add an explicit monotonicity check in `calculate_maximum_withdraw` immediately after reading both `ar` values:

```rust
let (deposit_ar, _, _, _) = extract_dao_data(deposit_header.dao());
let (withdrawing_ar, _, _, _) = extract_dao_data(withdrawing_header.dao());

// ar must be monotonically non-decreasing; if withdrawing_ar < deposit_ar,
// the DAO field is corrupted or the headers are mismatched.
if withdrawing_ar < deposit_ar {
    return Err(DaoError::InvalidOutPoint);
}
```

Additionally, consider adding a consensus-level check during block verification that each block's `ar` is `>= parent_ar`, analogous to the existing block-number and epoch continuity checks.

---

### Proof of Concept

Given:
- `deposit_header` at block 100 with `ar = 10_000_000_000_000_000` (normal mainnet value)
- `withdrawing_header` at block 200 with `ar = 1` (crafted/corrupted)
- A DAO cell with `capacity = 1_000_000_000_000` shannons and `occupied_capacity = 6_100_000_000` shannons

The calculation in `calculate_maximum_withdraw`:

```
counted_capacity = 1_000_000_000_000 - 6_100_000_000 = 993_900_000_000
withdraw_counted_capacity = 993_900_000_000 * 1 / 10_000_000_000_000_000 = 0  (integer truncation)
withdraw_capacity = 0 + 6_100_000_000 = 6_100_000_000 shannons
```

The depositor deposited ~1,000 CKB and receives back only ~6.1 CKB (the occupied capacity minimum), losing ~993.9 CKB of principal. The function returns `Ok(Capacity::shannons(6_100_000_000))` with no error. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** util/dao/src/lib.rs (L142-158)
```rust
        if deposit_header.number() >= withdrawing_header.number() {
            return Err(DaoError::InvalidOutPoint);
        }

        let (deposit_ar, _, _, _) = extract_dao_data(deposit_header.dao());
        let (withdrawing_ar, _, _, _) = extract_dao_data(withdrawing_header.dao());

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

**File:** util/dao/src/lib.rs (L256-261)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;
```

**File:** util/dao/utils/src/lib.rs (L104-111)
```rust
pub fn extract_dao_data(dao: Byte32) -> (u64, Capacity, Capacity, Capacity) {
    let data = dao.raw_data();
    let c = Capacity::shannons(LittleEndian::read_u64(&data[0..8]));
    let ar = LittleEndian::read_u64(&data[8..16]);
    let s = Capacity::shannons(LittleEndian::read_u64(&data[16..24]));
    let u = Capacity::shannons(LittleEndian::read_u64(&data[24..32]));
    (ar, c, s, u)
}
```
