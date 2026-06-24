Audit Report

## Title
Silent Truncating `u128→u64` Cast in `calculate_maximum_withdraw` Silently Returns Wrong Withdrawal Capacity — (File: `util/dao/src/lib.rs`)

## Summary
`DaoCalculator::calculate_maximum_withdraw` computes a u128 intermediate `withdraw_counted_capacity` and narrows it to u64 via a bare `as u64` truncating cast at line 156. If the intermediate exceeds `u64::MAX`, the high bits are silently discarded and the function returns `Ok(wrong_value)` instead of `Err(DaoError::Overflow)`. Every other u128→u64 narrowing in the same `impl` block uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`; this one site does not. The function is on the consensus-critical path for DAO field computation and transaction fee verification.

## Finding Description
At `util/dao/src/lib.rs` lines 152–156:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

The `as u64` cast is a Rust truncating (wrapping) cast — it silently discards bits above position 63. The subsequent `safe_add(occupied_capacity)` only guards against overflow in the final addition; it cannot detect the prior truncation. The function returns `Ok(truncated_value + occupied_capacity)` with no error signal.

By contrast, the three other u128→u64 narrowings in the same file all use the checked pattern:
- Line 204: `u64::try_from(reward128).map_err(|_| DaoError::Overflow)?`
- Line 245: `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?`
- Line 258: `u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?`

`calculate_maximum_withdraw` is called from two consensus-critical paths:
1. `transaction_maximum_withdraw` → `transaction_fee` (lines 30–36): used during block verification to validate DAO withdrawal transactions do not create capacity from nothing.
2. `transaction_maximum_withdraw` → `withdrawed_interests` → `dao_field_with_current_epoch` (lines 312–333): used to compute the `S_i` surplus field embedded in every block header's DAO field.

It is also called directly from the RPC `calculate_dao_maximum_withdraw`, which wallets use to determine withdrawal amounts.

## Impact Explanation
When truncation fires, `withdraw_capacity` is set to `(withdraw_counted_capacity mod 2^64) + occupied_capacity` — a value potentially orders of magnitude smaller than the correct amount.

**Consensus path — DAO field:** `withdrawed_interests` feeds the truncated `maximum_withdraw` into the `S_i` update for the block header. The DAO field written into the chain is incorrect. Since the DAO field drives all subsequent `ar`-based interest calculations, any node independently verifying the DAO field against the correct formula will compute a different value and reject the block, causing a consensus split. This matches the **Critical** impact: "Vulnerabilities which could easily cause consensus deviation."

**Economic path:** A wallet querying the RPC receives the truncated value and constructs a withdrawal transaction claiming only a fraction of the depositor's principal plus interest. The remainder is permanently unspendable. This matches the **Critical** impact: "Vulnerabilities which could easily damage CKB economy."

## Likelihood Explanation
Truncation requires `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX ≈ 1.844 × 10^19`.

- Maximum total CKB supply ≈ 3.36 × 10^18 shannons, so `counted_capacity ≤ 3.36 × 10^18`.
- Genesis `ar = 10^16` (`DEFAULT_GENESIS_ACCUMULATE_RATE`).
- For truncation with `counted_capacity` near total supply: `withdrawing_ar / deposit_ar > 1.844×10^19 / 3.36×10^18 ≈ 5.49`.
- At mainnet secondary issuance rate (~4%/year `ar` growth), this threshold is reached in approximately 43 years from genesis deposit.

The likelihood is low in the near term but the bug is **latent and deterministic**: it will trigger on a long-lived chain for any large depositor who holds through the threshold epoch. No attacker capability is required — a normal user making a large deposit and holding long enough will trigger it automatically. The inconsistency with every other narrowing cast in the same file confirms this is unintentional.

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
Using the existing test harness in `util/dao/src/tests.rs` (see `check_withdraw_calculation` at line 234):

1. Construct a deposit header with `ar = 5 × 10^16` (set via `pack_dao_data`).
2. Create a deposit cell with `counted_capacity = 4 × 10^18` shannons (achievable by setting `output.capacity()` accordingly in the test builder).
3. Construct a withdrawing header with `ar = 5.5 × 10^16`.
4. Call `DaoCalculator::calculate_maximum_withdraw`.
5. Intermediate: `withdraw_counted_capacity = 4×10^18 × 5.5×10^16 / 5×10^16 = 4.4×10^18`, which is within u64 range — no truncation yet.
6. Increase `counted_capacity` to `4.5 × 10^18` and keep the same ar ratio (1.1): `withdraw_counted_capacity = 4.5×10^18 × 1.1 = 4.95×10^18` — still within range.
7. Use `counted_capacity = 1.8 × 10^19` (near u64::MAX) and `withdrawing_ar / deposit_ar = 1.1`: `withdraw_counted_capacity = 1.98 × 10^19 > u64::MAX (1.844×10^19)`. The `as u64` cast yields `1.98×10^19 - 1.844×10^19 = 1.36×10^18`. The function returns `Ok(1.36×10^18 + occupied_capacity)` instead of `Err(Overflow)`, silently accepting a withdrawal that pays the user ~7% of what they are owed.

Assert that the current code returns `Ok(...)` where `u64::try_from` would return `Err(DaoError::Overflow)`, confirming the silent truncation.