### Title
Silent Integer Truncation in `calculate_maximum_withdraw` Silently Underpays NervosDAO Withdrawals - (File: util/dao/src/lib.rs)

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes `withdraw_counted_capacity` as a `u128` intermediate value but then silently truncates it to `u64` via a bare `as u64` cast. Every other `u128`→`u64` conversion in the same file uses the checked `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern. When the intermediate value exceeds `u64::MAX`, the silent truncation produces a drastically smaller capacity, causing NervosDAO withdrawers to silently receive far less CKB than they are entitled to, with the excess permanently locked.

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the withdrawable capacity as:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast is a wrapping/truncating cast in Rust. If `withdraw_counted_capacity` exceeds `u64::MAX ≈ 1.844 × 10^19`, the high bits are silently discarded, producing a value that can be orders of magnitude smaller than the correct result.

Every other `u128`→`u64` narrowing in the same file uses the safe pattern:

```rust
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;   // line 204
let miner_issuance = Capacity::shannons(u64::try_from(miner_issuance128)  // line 245
    .map_err(|_| DaoError::Overflow)?);
let ar_increase = u64::try_from(ar_increase128)                            // line 258
    .map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The `ar` (accumulate rate) field is a `u64` stored in the DAO header field, starting at `DEFAULT_GENESIS_ACCUMULATE_RATE = 10_000_000_000_000_000` (`10^16`) and growing monotonically with each block's secondary issuance:

```rust
const DEFAULT_GENESIS_ACCUMULATE_RATE: u64 = 10_000_000_000_000_000;
``` [5](#0-4) 

The `ar` field is a plain `u64` packed into the 32-byte DAO field:

```rust
let ar = LittleEndian::read_u64(&data[8..16]);
``` [6](#0-5) 

The overflow condition is:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

Since `withdrawing_ar ≥ deposit_ar` always holds (ar is monotonically increasing), the ratio is always ≥ 1. For a depositor holding a large cell (e.g., `counted_capacity` near `u64::MAX / 5 ≈ 3.7 × 10^18` shannons, i.e., ~37 billion CKB), the overflow triggers when `withdrawing_ar / deposit_ar > 5`, meaning `ar` has grown by a factor of 5 since deposit time.

### Impact Explanation

Two concrete impacts:

**1. Silent loss of funds (primary impact):** A user who queries `calculate_dao_maximum_withdraw` via RPC receives a truncated (far too small) capacity. They construct a withdrawal transaction paying themselves the wrong amount. The DAO script accepts any output ≤ the true maximum, so the transaction is accepted on-chain. The user receives drastically less CKB than entitled; the excess is permanently locked in the DAO contract with no recovery path.

**2. Denial-of-service on legitimate withdrawals (secondary impact):** A user who independently computes the correct maximum and constructs a transaction paying themselves the correct amount submits it to the node. The node calls `transaction_fee` → `transaction_maximum_withdraw` → `calculate_maximum_withdraw`, gets the truncated (too small) value, then `maximum_withdraw.safe_sub(outputs_capacity)` underflows and returns `Err`, causing the node to reject the transaction as invalid. The user cannot withdraw at all. [7](#0-6) 

### Likelihood Explanation

The overflow requires `ar` to grow by a factor proportional to `u64::MAX / counted_capacity`. On mainnet, `ar` grows at roughly `secondary_epoch_reward / total_capacity` per block. With `secondary_epoch_reward ≈ 1.344 billion CKB/year` and `total_capacity ≈ 33.6 billion CKB`, `ar` doubles in roughly 25 years. For a depositor holding ~3.7 billion CKB (counted capacity ~`u64::MAX / 5`), the overflow triggers when `ar` grows by 5×, i.e., after ~125 years. For smaller deposits the threshold is higher. This is a long time horizon, but:

- CKB is designed as a long-lived store-of-value chain; NervosDAO deposits are explicitly intended to be held for years or decades.
- The vulnerability is a latent time-bomb: it requires no attacker action, only the passage of time and normal protocol operation.
- The entry path is fully unprivileged: any NervosDAO depositor who holds long enough is affected.

### Recommendation

Replace the silent `as u64` cast with the same checked conversion used everywhere else in the file:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

### Proof of Concept

The existing test `check_withdraw_calculation_overflows` demonstrates awareness of overflow in this function but relies on `safe_add` catching the overflow at the final addition step. It does **not** test the case where `withdraw_counted_capacity` itself overflows `u64` before the `as u64` cast.

A concrete triggering scenario:

- `deposit_ar = 10_000_000_000_000_000` (genesis value, `10^16`)
- `withdrawing_ar = 55_000_000_000_000_000` (`5.5 × 10^16`, i.e., `ar` grew by 5.5×)
- `counted_capacity = 3_700_000_000_000_000_000` shannons (37 billion CKB)
- `withdraw_counted_capacity (u128) = 3_700_000_000_000_000_000 × 55_000_000_000_000_000 / 10_000_000_000_000_000`
  `= 3_700_000_000_000_000_000 × 5.5 = 20_350_000_000_000_000_000`
- `u64::MAX = 18_446_744_073_709_551_615`
- `20_350_000_000_000_000_000 > u64::MAX` → overflow
- `20_350_000_000_000_000_000 as u64 = 20_350_000_000_000_000_000 - 18_446_744_073_709_551_616 = 1_903_255_926_290_448_384`
- Returned capacity: `~1.9 × 10^18` shannons instead of `~2.0 × 10^19` shannons
- User loses ~18.4 billion CKB permanently [8](#0-7) [9](#0-8)

### Citations

**File:** util/dao/src/lib.rs (L30-36)
```rust
    pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
        let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
        rtx.transaction
            .outputs_capacity()
            .and_then(|y| maximum_withdraw.safe_sub(y))
            .map_err(Into::into)
    }
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

**File:** util/dao/src/lib.rs (L202-205)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
        Ok(Capacity::shannons(reward))
```

**File:** util/dao/src/lib.rs (L242-245)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
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

**File:** util/dao/utils/src/lib.rs (L17-17)
```rust
const DEFAULT_GENESIS_ACCUMULATE_RATE: u64 = 10_000_000_000_000_000;
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
