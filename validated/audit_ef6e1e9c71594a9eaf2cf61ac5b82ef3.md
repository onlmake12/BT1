### Title
Silent Truncating Cast in `calculate_maximum_withdraw` Produces Incorrect DAO Withdrawal Capacity — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` in `util/dao/src/lib.rs` computes the maximum withdrawable capacity for a NervosDAO cell using a u128 intermediate value, then converts it to u64 with a bare `as u64` truncating cast. Every other analogous u128→u64 conversion in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. The inconsistency means that if the intermediate product ever exceeds `u64::MAX`, the result is silently wrapped rather than rejected, producing an arithmetically incorrect capacity value that propagates into both on-chain transaction verification and the `calculate_dao_maximum_withdraw` RPC response.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
// lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← truncating cast
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast silently discards the upper 64 bits when `withdraw_counted_capacity > u64::MAX`. Every other u128→u64 narrowing in the same file uses the checked form:

```rust
// line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// line 245
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)

// line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

`calculate_maximum_withdraw` is the sole exception. The intermediate product is `counted_capacity_shannons × withdrawing_ar`, where `withdrawing_ar` is the DAO accumulation rate (a u64 that starts at `DEFAULT_GENESIS_ACCUMULATE_RATE = 10_000_000_000_000_000` and grows monotonically). The product can reach up to `u64::MAX × u64::MAX ≈ 3.4 × 10^38`, which fits in u128 but not in u64 after division if the ratio `withdrawing_ar / deposit_ar` is large enough.

---

### Impact Explanation

`calculate_maximum_withdraw` is called in two reachable paths:

1. **On-chain verification** — `transaction_maximum_withdraw` → `transaction_fee` is invoked by the block verifier for every DAO withdrawal transaction. A truncated `withdraw_counted_capacity` produces a wrong `maximum_withdraw`, which then feeds into `maximum_withdraw.safe_sub(outputs_capacity)`. If the truncated value is smaller than the actual value, a legitimate withdrawal transaction is rejected. If the lower 64 bits of the wrapped value happen to be larger than `outputs_capacity`, an over-withdrawal transaction could pass fee validation. [5](#0-4) 

2. **RPC** — `calculate_dao_maximum_withdraw` exposes this function directly to any RPC caller. A truncated return value misleads wallets and users about the actual withdrawable amount. [6](#0-5) 

---

### Likelihood Explanation

For `withdraw_counted_capacity` to exceed `u64::MAX`, the ratio `withdrawing_ar / deposit_ar` must exceed approximately `u64::MAX / counted_capacity_max`. With the total CKB supply capped at ~33.6 billion CKB (~3.36 × 10^18 shannons) and `u64::MAX ≈ 1.84 × 10^19`, the ratio must exceed ~5.5×. Given the current secondary issuance schedule, reaching that ratio would take an extremely long time under normal operation. The likelihood of triggering the overflow on mainnet in the near term is therefore very low. However, the code is demonstrably inconsistent with every other u128→u64 conversion in the same file, and the absence of a checked conversion means the error would be silent and undetectable at runtime.

---

### Recommendation

Replace the bare truncating cast with the same checked pattern used everywhere else in the file:

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

This makes the function consistent with `secondary_block_reward`, `dao_field_with_current_epoch`, and the rest of the DAO calculator, and ensures that any future economic conditions that push the intermediate value past `u64::MAX` produce a clean error rather than a silently wrong capacity.

---

### Proof of Concept

The inconsistency is directly visible by comparing the three checked conversions at lines 204, 245, and 258 with the unchecked cast at line 156 within the same source file. [7](#0-6) 

A concrete numeric demonstration:

- Suppose `counted_capacity = u64::MAX` (≈ 1.84 × 10^19 shannons, hypothetically)
- `withdrawing_ar = 6 × deposit_ar` (accumulation rate grew 6×)
- Intermediate: `u64::MAX × 6 × deposit_ar / deposit_ar = 6 × u64::MAX ≈ 1.1 × 10^20` — exceeds u64::MAX
- `as u64` yields `5 × u64::MAX + 5 = u64::MAX - 1` (lower 64 bits of `6 × u64::MAX`), which is close to `u64::MAX` — a value that is arithmetically wrong but passes silently
- `u64::try_from(...)` would return `DaoError::Overflow` and reject the transaction cleanly

The `calculate_maximum_withdraw` function is reachable by any unprivileged transaction sender who deposits into and later withdraws from the NervosDAO, making this an externally triggerable path with no privileged access required.

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

**File:** util/dao/src/lib.rs (L126-159)
```rust
    /// Calculate maximum withdraw capacity of a deposited dao output
    pub fn calculate_maximum_withdraw(
        &self,
        output: &CellOutput,
        output_data_capacity: Capacity,
        deposit_header_hash: &Byte32,
        withdrawing_header_hash: &Byte32,
    ) -> Result<Capacity, DaoError> {
        let deposit_header = self
            .data_loader
            .get_header(deposit_header_hash)
            .ok_or(DaoError::InvalidHeader)?;
        let withdrawing_header = self
            .data_loader
            .get_header(withdrawing_header_hash)
            .ok_or(DaoError::InvalidHeader)?;
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
    }
```

**File:** util/dao/src/lib.rs (L202-205)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
        Ok(Capacity::shannons(reward))
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

**File:** rpc/src/module/experiment.rs (L167-196)
```rust
    /// Get fee estimates.
    ///
    /// ## Params
    ///
    /// * `estimate_mode` - The fee estimate mode.
    ///
    ///   Default: `no_priority`.
    ///
    /// * `enable_fallback` - True to enable a simple fallback algorithm, when lack of historical empirical data to estimate fee rates with configured algorithm.
    ///
    ///   Default: `true`.
    ///
    /// ### The fallback algorithm
    ///
    /// Since CKB transaction confirmation involves a two-step process—1) propose and 2) commit, it is complex to
    /// predict the transaction fee accurately with the expectation that it will be included within a certain block height.
    ///
    /// This algorithm relies on two assumptions and uses a simple strategy to estimate the transaction fee: 1) all transactions
    /// in the pool are waiting to be proposed, and 2) no new transactions will be added to the pool.
    ///
    /// In practice, this simple algorithm should achieve good accuracy fee rate and running performance.
    ///
    /// ## Returns
    ///
    /// The estimated fee rate in shannons per kilobyte.
    ///
    /// ## Examples
    ///
    /// Request
    ///
```
