Audit Report

## Title
Silent Truncating `as u64` Cast in `calculate_maximum_withdraw` Silently Corrupts NervosDAO Withdrawal Capacity — (File: `util/dao/src/lib.rs`)

## Summary
`DaoCalculator::calculate_maximum_withdraw` computes a u128 intermediate `withdraw_counted_capacity` and narrows it to u64 via a bare `as u64` truncating cast at line 156. If the intermediate exceeds `u64::MAX`, the high bits are silently discarded and the function returns `Ok` with a drastically undervalued capacity. Every other u128→u64 narrowing in the same `impl` block uses the checked `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern; this one site does not. The function sits on the consensus-critical path for DAO field computation and transaction fee verification.

## Finding Description
In `util/dao/src/lib.rs` lines 152–156:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

The `as u64` cast is a Rust wrapping/truncating cast. When `withdraw_counted_capacity > u64::MAX`, the result is `withdraw_counted_capacity % 2^64`, a value potentially orders of magnitude smaller than the correct amount. The subsequent `safe_add(occupied_capacity)` only guards against overflow in the final addition and cannot detect the prior silent truncation. The function returns `Ok(truncated_value)` instead of `Err(DaoError::Overflow)`.

By contrast, all other u128→u64 narrowings in the same file use the checked pattern:
- Line 204: `u64::try_from(reward128).map_err(|_| DaoError::Overflow)?`
- Line 245: `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?`
- Line 258: `u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?`

The `DaoError::Overflow` variant exists precisely for this purpose (confirmed in `util/dao/utils/src/error.rs` lines 36–38).

`calculate_maximum_withdraw` is called from two consensus-critical paths:
1. `transaction_maximum_withdraw` → `transaction_fee` (lines 30–36): used during block verification to validate DAO withdrawal transactions do not create capacity from nothing.
2. `transaction_maximum_withdraw` → `withdrawed_interests` → `dao_field_with_current_epoch` (lines 312–333): used to compute the `S_i` surplus field embedded in every block header's DAO field.

## Impact Explanation
When truncation fires, `withdraw_capacity` is set to `(withdraw_counted_capacity mod 2^64) + occupied_capacity`, a value far smaller than the correct amount.

**Consensus path — DAO field:** `withdrawed_interests` feeds the truncated `maximum_withdraw` into the `S_i` update for the block header. The DAO field written into the chain is incorrect. Nodes that independently recompute the DAO field will reject the block, causing a consensus split. This matches the allowed Critical impact: **"Vulnerabilities which could easily cause consensus deviation."**

**Economic path — transaction fee check:** `transaction_fee` computes `maximum_withdraw - outputs_capacity`. A wallet querying the RPC (which calls the same function) will set `outputs_capacity` to the truncated amount. The depositor receives a tiny fraction of their principal plus interest; the remainder is permanently unspendable.

## Likelihood Explanation
Truncation requires `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`.

- Genesis `ar` = `10^16` (confirmed: `DEFAULT_GENESIS_ACCUMULATE_RATE = 10_000_000_000_000_000` in `util/dao/utils/src/lib.rs` line 17).
- Maximum realistic `counted_capacity` ≈ 3.36 × 10^18 shannons (total CKB supply, confirmed from mainnet genesis DAO data in `util/dao/utils/src/lib.rs` line 140: `c = 3360000145238488200`).
- For truncation: `withdrawing_ar / deposit_ar` must exceed `u64::MAX / 3.36×10^18 ≈ 5.49`.
- At mainnet secondary issuance rate (~4%/year `ar` growth), this threshold is reached in approximately 43–50 years.

The likelihood is low in the near term but the bug is **latent and deterministic**: it will trigger on a long-lived chain for any large depositor who holds through the threshold epoch. The inconsistency with every other narrowing cast in the same file confirms this is unintentional, not a deliberate design choice. No attacker capability is required — the truncation fires automatically based on chain state.

Note: The PoC in the submission uses `counted_capacity = 1.8 × 10^19` which exceeds the total CKB supply and u64::MAX, making that specific step unrealistic. The realistic trigger path (large depositor + ~50 years of `ar` growth) is the correct scenario.

## Recommendation
Replace the truncating cast with the same checked pattern used everywhere else in the file:

```rust
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    ).safe_add(occupied_capacity)?;
```

This makes `calculate_maximum_withdraw` return `Err(DaoError::Overflow)` instead of silently returning a wrong value, consistent with `secondary_block_reward` and `dao_field_with_current_epoch`.

## Proof of Concept
Add a unit test to `util/dao/src/tests.rs` mirroring the existing `check_withdraw_calculation` test (lines 234–292) but with manipulated DAO header values:

1. Set `deposit_ar` to `10^16` (genesis value).
2. Set `withdrawing_ar` to `5.5 × 10^16` (above the truncation threshold for large deposits).
3. Set `output.capacity()` to a value such that `counted_capacity` is large enough that `counted_capacity * 5.5×10^16 / 10^16 > u64::MAX`. For example, `counted_capacity = 3.36 × 10^18` and `withdrawing_ar / deposit_ar = 5.49` gives `withdraw_counted_capacity ≈ 1.845 × 10^19 > u64::MAX`.
4. Call `calculator.calculate_maximum_withdraw(...)`.
5. With the current code, the function returns `Ok(truncated_value)` — a value far below the correct withdrawal amount.
6. With the fix applied, the function returns `Err(DaoError::Overflow)`.

The `pack_dao_data` helper used in existing tests allows direct injection of arbitrary `ar` values into test block headers, making this fully reproducible without a live chain.