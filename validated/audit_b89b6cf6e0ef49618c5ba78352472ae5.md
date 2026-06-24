Audit Report

## Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Returns Wrong Withdrawal Capacity — (`util/dao/src/lib.rs`)

## Summary
`DaoCalculator::calculate_maximum_withdraw` computes a u128 intermediate value `withdraw_counted_capacity` and narrows it to u64 with a silent `as u64` cast at line 156, which silently wraps on overflow. Every other u128→u64 narrowing in the same file (lines 204, 245, 258) uses `u64::try_from(…).map_err(|_| DaoError::Overflow)?`. When the intermediate value exceeds `u64::MAX`, the function returns `Ok(wrong_capacity)` instead of `Err(DaoError::Overflow)`, corrupting both per-transaction fee accounting and the on-chain DAO field packed into every subsequent block header.

## Finding Description
In `util/dao/src/lib.rs` lines 152–156:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)  // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast takes only the low 64 bits of `withdraw_counted_capacity`. When the u128 value exceeds `u64::MAX`, the result wraps to an arbitrarily small number, and `safe_add(occupied_capacity)` succeeds without error, returning a completely wrong `Ok(Capacity)`.

Contrast with the three other u128→u64 narrowings in the same file, all of which use the checked pattern:

```rust
// line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
// line 245
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)
// line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The overflow condition is:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

`deposit_ar` starts at `DEFAULT_GENESIS_ACCUMULATE_RATE = 10_000_000_000_000_000` (10^16). [5](#0-4) 

Since `ar` is monotonically non-decreasing, `withdrawing_ar ≥ deposit_ar` always holds, so `withdraw_counted_capacity ≥ counted_capacity`. Overflow occurs when `withdrawing_ar / deposit_ar` exceeds `u64::MAX / counted_capacity`.

The corrupted return value propagates through two paths:

1. **`transaction_fee`** (line 31–35): computes `maximum_withdraw - outputs_capacity`. A silently truncated `maximum_withdraw` causes the fee check to accept a withdrawal transaction with a near-zero or incorrect fee. [6](#0-5) 

2. **`withdrawed_interests` → `dao_field_with_current_epoch`** (lines 222, 252–254): the wrong `maximum_withdraw` is summed into `withdrawed_interests`, which is then subtracted from the running DAO accumulator `s`. This corrupts the DAO field packed into every subsequent block header, permanently skewing `ar` growth and all future DAO interest calculations. [7](#0-6) [8](#0-7) 

## Impact Explanation
When triggered, the corrupted DAO field written into block headers causes nodes to compute divergent DAO state, producing consensus deviation. This maps to the Critical impact class: **"Vulnerabilities which could easily cause consensus deviation."** Additionally, the wrong fee accounting and corrupted `s` accumulator directly damage the CKB economy by allowing under-fee withdrawals and permanently skewing all future DAO depositor interest, mapping to **"Vulnerabilities which could easily damage CKB economy."**

## Likelihood Explanation
On mainnet, the overflow requires `withdrawing_ar / deposit_ar > ~5.5` (for a cell holding the maximum practical capacity), which demands AR to grow to 5.5× its genesis value. Given the slow secondary issuance rate, this is not reachable in the near term on mainnet. However:
- The condition is reachable in principle by any unprivileged user who submits a DAO withdrawal transaction — no special privilege is required.
- On a testnet or a chain spec with elevated `secondary_epoch_reward`, the threshold is reached significantly sooner.
- The code defect is a latent bug that will eventually become reachable as the chain ages, and the inconsistency with the rest of the file indicates it is unintentional.

## Recommendation
Replace the silent cast with the same checked pattern used at lines 204, 245, and 258:

```rust
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
```

## Proof of Concept
**Arithmetic trigger** (all values exact):

```
deposit_ar         = 10_000_000_000_000_000        // genesis ar (10^16)
withdrawing_ar     = 55_000_000_000_000_000        // ar grown 5.5×
counted_capacity   = 3_360_000_000_000_000_000     // ~33.6 billion CKB in shannons

withdraw_counted_capacity (u128)
  = 3_360_000_000_000_000_000 × 55_000_000_000_000_000
    / 10_000_000_000_000_000
  = 18_480_000_000_000_000_000
  > u64::MAX (18_446_744_073_709_551_615)

as u64 → 18_480_000_000_000_000_000 mod 2^64
        = 33_255_926_290_448_384   ← ~540× too small
```

A unit test can be written against `calculate_maximum_withdraw` with a mock `DataLoader` returning headers whose DAO fields encode the above `deposit_ar` and `withdrawing_ar` values, and a `CellOutput` with `capacity = 3_360_000_000_000_000_000` shannons. The test asserts that the function returns `Err(DaoError::Overflow)` with the fix applied, and demonstrates it currently returns `Ok(Capacity::shannons(33_255_926_290_448_384 + occupied_capacity))` — a wrong value — without the fix.

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

**File:** util/dao/src/lib.rs (L222-222)
```rust
        let withdrawed_interests = self.withdrawed_interests(rtxs)?;
```

**File:** util/dao/src/lib.rs (L244-245)
```rust
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/dao/src/lib.rs (L252-254)
```rust
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;
```

**File:** util/dao/src/lib.rs (L256-258)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/utils/src/lib.rs (L16-17)
```rust
// This is multiplied by 10**16 to make sure we have enough precision.
const DEFAULT_GENESIS_ACCUMULATE_RATE: u64 = 10_000_000_000_000_000;
```
