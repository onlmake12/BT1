Audit Report

## Title
Silent `u128→u64` Truncation in `calculate_maximum_withdraw` Returns Wrong Withdrawal Capacity — (`File: util/dao/src/lib.rs`)

## Summary

`DaoCalculator::calculate_maximum_withdraw` computes the maximum withdrawable capacity using a `u128` intermediate value but casts it to `u64` with a bare `as u64` at line 156, silently discarding high bits on overflow. Every other `u128→u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. When the intermediate result exceeds `u64::MAX`, the function returns a silently wrong (too-small) capacity instead of an error, causing downstream fee verification to reject the withdrawal transaction and permanently locking the deposited funds.

## Finding Description

In `util/dao/src/lib.rs`, lines 152–156, the computation is:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
```

The `as u64` cast wraps silently: if `withdraw_counted_capacity > u64::MAX`, only the low 64 bits are kept. The returned `withdraw_capacity` is then far smaller than the true maximum.

Every other `u128→u64` narrowing in the same file is done safely:
- Line 204: `let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;`
- Line 245: `Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)`
- Line 258: `let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;`

The overflow condition is `counted_capacity * withdrawing_ar > u64::MAX * deposit_ar`. When triggered, the truncated value may be small enough that `safe_add(occupied_capacity)` succeeds, returning a silently wrong result with no error.

The existing test `check_withdraw_calculation_overflows` (lines 296–350 of `util/dao/src/tests.rs`) only covers the sub-case where the truncated value plus `occupied_capacity` still overflows `u64` (triggering `safe_add`'s error). It does not cover the silent-success case where truncation produces a small value that passes `safe_add` but is incorrect.

The call chain is:
1. `calculate_maximum_withdraw` → returns silently wrong capacity
2. `transaction_maximum_withdraw` → accumulates wrong value
3. `transaction_fee` → calls `maximum_withdraw.safe_sub(outputs_capacity)` → fails with `CapacityError::Overflow` because the truncated maximum is smaller than the actual output capacity
4. `TransactionVerifier` rejects the withdrawal transaction

The same function is also exposed via the `calculate_dao_maximum_withdraw` RPC (lines 259–267 of `rpc/src/module/experiment.rs`), which would silently return a wrong value to callers constructing withdrawal transactions.

## Impact Explanation

A DAO depositor with a sufficiently large cell who waits long enough for the accumulate rate to grow will find their withdrawal transaction permanently rejected by every node. The deposited CKB is locked in the DAO cell with no valid withdrawal path. The error is silent — the node returns a capacity value rather than an overflow error — so wallet software and RPC callers receive no indication that the computed maximum is wrong. This constitutes concrete, irreversible economic damage to depositors, matching the allowed impact: **Vulnerabilities which could easily damage CKB economy (Critical, 15001–25000 points)**, though the "easily" qualifier is tempered by the low near-term likelihood on mainnet.

## Likelihood Explanation

The overflow condition requires `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. The total CKB supply is approximately 3.36×10¹⁸ shannons, so a single cell can hold at most that amount. The AR starts at approximately 10¹⁶ and grows slowly via secondary issuance; a 5.5× multiple would take decades on mainnet under standard parameters. Likelihood is therefore low in the near term on mainnet. However, the condition is already reachable on any chain (devnet, testnet) where the genesis AR or secondary issuance parameters are set to non-standard values, and the defect is a confirmed code inconsistency that will eventually become reachable as the chain ages.

## Recommendation

Replace the silent cast with the same checked pattern used everywhere else in the file:

```rust
// Before (line 156):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After:
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?
```

Additionally, add a test case that sets `deposit_ar`, `withdrawing_ar`, and `counted_capacity` such that `withdraw_counted_capacity > u64::MAX` but the truncated value plus `occupied_capacity` does not overflow `u64`, verifying that the function returns `Err(DaoError::Overflow)` rather than a silently wrong `Ok(...)`.

## Proof of Concept

Using the existing test harness pattern from `check_withdraw_calculation_overflows`:

```
deposit_ar         = 10_000_000_000_000_000   (10^16)
withdrawing_ar     = 55_000_000_000_000_000   (5.5× deposit_ar)
counted_capacity   = 3_360_000_000_000_000_000 (3.36×10^18 shannons, ~total supply)

withdraw_counted_capacity (u128)
  = 3_360_000_000_000_000_000 * 55_000_000_000_000_000 / 10_000_000_000_000_000
  = 18_480_000_000_000_000_000   (> u64::MAX ≈ 18_446_744_073_709_551_615)

as u64 truncation:
  18_480_000_000_000_000_000 mod 2^64
  = 18_480_000_000_000_000_000 - 18_446_744_073_709_551_616
  = 33_255_926_290_448_384   (≈ 3.3×10^16, far below true value)

safe_add(occupied_capacity) succeeds → returns ~3.3×10^16 shannons (wrong)
True maximum should be ~1.848×10^19 shannons

transaction_fee: maximum_withdraw.safe_sub(outputs_capacity)
  → outputs_capacity (correct) >> truncated maximum → DaoError::Overflow
  → withdrawal transaction permanently rejected
```

A unit test with these parameters, asserting `result == Err(DaoError::Overflow)`, would fail against the current code (returning `Ok(...)` with a wrong value) and pass after the fix.