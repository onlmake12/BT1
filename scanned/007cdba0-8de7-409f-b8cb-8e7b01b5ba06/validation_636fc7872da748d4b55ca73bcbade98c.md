### Title
Systematic Floor-Rounding in NervosDAO Withdrawal and AR Accumulator Calculations Causes Depositors to Receive Less Than Mathematically Exact Interest — (File: `util/dao/src/lib.rs`)

---

### Summary

The NervosDAO withdrawal calculation and the per-block accumulation-rate (AR) update both use integer division that truncates (floors) the result. Every DAO withdrawal loses up to 1 Shannon from the direct withdrawal truncation, and the AR accumulator grows slightly slower than the mathematically exact value with each block, compounding the underestimate over the deposit lifetime. Both effects are systematic and disadvantage every DAO depositor.

---

### Finding Description

**Root cause 1 — `calculate_maximum_withdraw` (direct withdrawal truncation)**

In `util/dao/src/lib.rs`, the withdrawable interest-bearing capacity is computed as:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
``` [1](#0-0) 

Rust integer division truncates toward zero. The exact mathematical value is `counted_capacity × withdrawing_ar / deposit_ar`, but the returned value is `⌊counted_capacity × withdrawing_ar / deposit_ar⌋`. The depositor loses the fractional Shannon remainder on every withdrawal.

**Root cause 2 — `dao_field_with_current_epoch` AR accumulator truncation**

The per-block AR increase is computed as:

```rust
let ar_increase128 =
    u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
``` [2](#0-1) 

This also truncates. Because `withdrawing_ar` in `calculate_maximum_withdraw` is the product of N such truncated increments, the AR is systematically underestimated relative to the exact value. A lower `withdrawing_ar` directly reduces the withdrawal amount returned to the depositor.

**Root cause 3 — `miner_issuance128` truncation (secondary effect)**

The miner's share of secondary issuance is also truncated:

```rust
let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
    / u128::from(parent_c.as_u64());
let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;
``` [3](#0-2) 

Here the truncation favors the NervosDAO (the remainder goes to `nervosdao_issuance`), partially offsetting root cause 2. However, the AR truncation (root cause 2) is the dominant effect because it compounds multiplicatively across every block in the deposit window.

**Supporting utility — `safe_mul_ratio` also truncates**

The `Capacity::safe_mul_ratio` helper used throughout reward calculations performs `(self * numer) / denom` with integer truncation:

```rust
pub fn safe_mul_ratio(self, ratio: Ratio) -> Result<Self> {
    self.0
        .checked_mul(ratio.numer())
        .and_then(|ret| ret.checked_div(ratio.denom()))
        .map(Capacity::shannons)
        .ok_or(Error::Overflow)
}
``` [4](#0-3) 

This is used in `txs_fees` and `proposal_reward` in `util/reward-calculator/src/lib.rs` to compute the 40% proposer share:

```rust
tx_fee
    .safe_mul_ratio(consensus.proposer_reward_ratio())
    .and_then(|proposer| {
        tx_fee
            .safe_sub(proposer)
            .and_then(|miner| acc.safe_add(miner))
    })
``` [5](#0-4) 

For very small fees (1–2 Shannons), `1 × 4 / 10 = 0`, so the proposer receives 0 instead of 40%, a 100% loss of their share. In practice the minimum fee rate makes sub-10-Shannon fees rare, but the truncation is structurally present.

---

### Impact Explanation

Every NervosDAO depositor who withdraws receives at most 1 Shannon less than the mathematically exact amount from the direct truncation in `calculate_maximum_withdraw`. Additionally, because the AR accumulator is truncated on every block, the `withdrawing_ar` value used at withdrawal time is lower than the exact value by up to N units (where N is the number of blocks since genesis). For a deposit of D Shannons held for N blocks, the cumulative AR-induced shortfall is approximately `D × N / parent_c` Shannons. With `parent_c` on the order of `10^18` Shannons (total CKB supply in Shannons), this remains small in absolute terms but is systematic and non-zero for every depositor. The effect is analogous to the external report: integer division floors the output of a financial formula, and the depositor absorbs the rounding loss on every operation.

---

### Likelihood Explanation

The truncation is unconditional — it fires on every block (AR update) and on every DAO withdrawal. Any user who deposits CKB into the NervosDAO and later withdraws is affected. No special conditions, privileges, or attacker cooperation are required. The entry path is the standard DAO deposit/withdraw transaction flow, reachable by any unprivileged transaction sender.

---

### Recommendation

Apply ceiling division (`(a * b + c - 1) / c`) in `calculate_maximum_withdraw` for `withdraw_counted_capacity` so that depositors receive the rounded-up Shannon rather than the truncated one. For the AR accumulator in `dao_field_with_current_epoch`, consider whether ceiling rounding is appropriate for `ar_increase128`; rounding up there would cause the AR to grow at least as fast as the exact value, ensuring depositors are never systematically shortchanged. The existing comment in `txs_fees` already acknowledges rounding sensitivity (`"Be careful of the rounding"`), confirming the developers are aware of the class of issue.

---

### Proof of Concept

**Direct withdrawal truncation:**

Suppose a depositor holds `counted_capacity = 3_900_000_001` Shannons, `deposit_ar = 10_000_000_000_000_000` (10^16), and `withdrawing_ar = 10_000_000_000_000_001` (one unit of AR growth).

```
withdraw_counted_capacity
  = 3_900_000_001 × 10_000_000_000_000_001 / 10_000_000_000_000_000
  = floor(3_900_000_001.0000000003900000001)
  = 3_900_000_001          ← depositor loses the fractional Shannon
```

The exact value is `3_900_000_001 + 3_900_000_001/10^16 ≈ 3_900_000_001.00000000039`, but the depositor receives only `3_900_000_001`.

**AR accumulator compounding:**

Each block, `ar_increase128 = parent_ar × g2 / parent_c` is truncated. If `parent_ar × g2` is not exactly divisible by `parent_c`, the AR grows by one unit less than exact. Over 1,000,000 blocks, `withdrawing_ar` can be up to 1,000,000 units below the exact value. For the same deposit above:

```
shortfall ≈ 3_900_000_001 × 1_000_000 / 10^16 ≈ 0.39 Shannons
```

Still sub-Shannon in absolute terms, but the mechanism is structurally identical to the external report: the final division in a financial formula is floored, and the user absorbs the loss on every interaction.

### Citations

**File:** util/dao/src/lib.rs (L152-154)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
```

**File:** util/dao/src/lib.rs (L242-246)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
        let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;
```

**File:** util/dao/src/lib.rs (L256-258)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/occupied-capacity/core/src/units.rs (L149-155)
```rust
    pub fn safe_mul_ratio(self, ratio: Ratio) -> Result<Self> {
        self.0
            .checked_mul(ratio.numer())
            .and_then(|ret| ret.checked_div(ratio.denom()))
            .map(Capacity::shannons)
            .ok_or(Error::Overflow)
    }
```

**File:** util/reward-calculator/src/lib.rs (L148-155)
```rust
                tx_fee
                    .safe_mul_ratio(consensus.proposer_reward_ratio())
                    .and_then(|proposer| {
                        tx_fee
                            .safe_sub(proposer)
                            .and_then(|miner| acc.safe_add(miner))
                    })
            })
```
