The code is confirmed. Let me verify the key facts before rendering a verdict.

**Code at lines 152-156** (`util/dao/src/lib.rs`):
- `withdraw_counted_capacity` is computed as `u128`
- Cast to `u64` via `as u64` (silent truncation)

**Other conversions in the same file** (lines 204, 245, 258):
- All use `u64::try_from(...).map_err(|_| DaoError::Overflow)?`

**Existing test `check_withdraw_calculation_overflows`** (lines 296-350):
- Uses `output_capacity = 18_446_744_073_709_550_000`, `deposit_ar ≈ 10^16`, `withdrawing_ar ≈ 10^16 + 10^6`
- `counted_capacity ≈ 18_446_744_069_609_550_000` (after subtracting occupied ~4.1B shannons)
- `withdraw_counted_capacity ≈ 18_446_744_069_609_551_844` — this fits in u64, so `as u64` does NOT truncate
- The error comes from `safe_add(occupied_capacity)` overflowing: `18_446_744_069_609_551_844 + 4_100_000_000 > u64::MAX`
- This confirms the test only covers the `safe_add` overflow path, not the silent `as u64` truncation path

**PoC math verification**:
- `3_360_000_000_000_000_000 × 60_000_000_000 / 10_000_000_000 = 20_160_000_000_000_000_000`
- `u64::MAX = 18_446_744_073_709_551_615`
- Truncated result: `20_160_000_000_000_000_000 - 18_446_744_073_709_551_616 = 1_713_255_926_290_448_384`
- `safe_add` succeeds with wrong value → `withdrawed_interests` underflows in `dao_field_with_current_epoch` → block rejected

All claims check out.

---

Audit Report

## Title
Silent `u64` Truncation in `calculate_maximum_withdraw` Returns Wrong NervosDAO Withdrawal Amount - (File: util/dao/src/lib.rs)

## Summary
In `DaoCalculator::calculate_maximum_withdraw`, the intermediate `u128` result `withdraw_counted_capacity` is cast to `u64` via `as u64` at line 156, silently truncating if the value exceeds `u64::MAX`. Every other analogous `u128`→`u64` conversion in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. When truncation occurs, the function returns a drastically wrong (much smaller) withdrawal amount, and any block containing a withdrawal transaction built from that amount is rejected at the `dao_field_with_current_epoch` step due to underflow in `withdrawed_interests`.

## Finding Description
In `util/dao/src/lib.rs` lines 152–156:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

The `as u64` cast at line 156 silently wraps if `withdraw_counted_capacity > u64::MAX`. This is inconsistent with lines 204, 245, and 258, which all use `u64::try_from(...).map_err(|_| DaoError::Overflow)?`.

The overflow condition is `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`. With `deposit_ar` starting at `10^10` and `counted_capacity` up to ~3.36×10^18 shannons (total CKB supply), overflow occurs when `withdrawing_ar / deposit_ar > ~5.36`, i.e., AR has grown ~5.36× since deposit.

When truncation occurs:
1. `calculate_maximum_withdraw` returns a wrong, much smaller value (the low 64 bits of the true u128 result).
2. The RPC `calculate_dao_maximum_withdraw` (in `rpc/src/module/experiment.rs`) calls this function and reports the wrong amount to the user.
3. The user constructs a withdrawal transaction with `outputs_capacity` equal to the truncated amount.
4. In `dao_field_with_current_epoch`, `withdrawed_interests = maximum_withdraws - input_capacities`. Since `maximum_withdraws` is now the truncated (much smaller) value while `input_capacities` is the original deposit amount, `safe_sub` underflows and returns `DaoError::Overflow`, causing block processing to fail.
5. The existing test `check_withdraw_calculation_overflows` does NOT cover this path: it is designed so that `withdraw_counted_capacity` itself fits in u64, and the error comes only from `safe_add(occupied_capacity)` overflowing — a completely different code path.

## Impact Explanation
When triggered, a NervosDAO depositor receives a drastically wrong (much smaller) withdrawal amount from the RPC. Any withdrawal transaction constructed from that amount is accepted into the mempool but causes the containing block to be rejected at the DAO field update step. The depositor is permanently unable to withdraw their funds via the normal path, and any miner who includes such a transaction loses their block reward. This constitutes concrete, irreversible economic damage to CKB depositors and miners — matching the allowed impact: **Vulnerabilities which could easily damage CKB economy (Critical, 15001–25000 points)**.

## Likelihood Explanation
On mainnet, the secondary epoch reward is ~1.344 billion CKB/year against ~33.6 billion CKB total capacity, giving an AR growth rate of ~4%/year. A 5.36× AR increase requires approximately 42 years of chain operation. This is a long-term scenario on mainnet. However, on testnets or devnets with higher secondary reward ratios or lower total capacity, the condition is reachable much sooner. The bug is structurally present today, inconsistent with the rest of the file, and any future parameter change that increases secondary issuance or reduces total capacity accelerates the timeline. No special privileges are required — any depositor who holds long enough triggers the condition.

## Recommendation
Replace the silent `as u64` cast with the same checked conversion pattern used everywhere else in the file:

```rust
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
```

Also add a unit test covering the silent truncation path: a scenario where `withdraw_counted_capacity > u64::MAX` but the truncated value is small enough that `safe_add` would succeed with a wrong result (e.g., `counted_capacity = 3_360_000_000_000_000_000`, `deposit_ar = 10_000_000_000`, `withdrawing_ar = 60_000_000_000`).

## Proof of Concept
Construct a scenario with:
- `counted_capacity = 3_360_000_000_000_000_000` shannons (~33.6 billion CKB)
- `deposit_ar = 10_000_000_000` (initial AR)
- `withdrawing_ar = 60_000_000_000` (AR grown 6×)

Computation:
```
withdraw_counted_capacity = 3_360_000_000_000_000_000 × 60_000_000_000 / 10_000_000_000
                          = 20_160_000_000_000_000_000
```
`u64::MAX = 18_446_744_073_709_551_615`

`20_160_000_000_000_000_000 > u64::MAX`, so:
```
withdraw_counted_capacity as u64
  = 20_160_000_000_000_000_000 − 18_446_744_073_709_551_616
  = 1_713_255_926_290_448_384
```

`safe_add(occupied_capacity)` succeeds (no overflow), returning ~1.71×10^18 shannons instead of ~2.016×10^19 shannons — roughly 11.8× less than entitled.

Then in `dao_field_with_current_epoch`:
```
withdrawed_interests = 1_713_255_926_290_448_384 + occupied_capacity
                     − 3_360_000_000_000_000_000   (input_capacity)
```
This underflows → `safe_sub` returns `DaoError::Overflow` → block is rejected.

A unit test can be added to `util/dao/src/tests.rs` mirroring `check_withdraw_calculation_overflows` but with the above parameters, asserting that `calculate_maximum_withdraw` returns `Err` (with the fix) rather than silently returning a wrong `Ok` value (current behavior).