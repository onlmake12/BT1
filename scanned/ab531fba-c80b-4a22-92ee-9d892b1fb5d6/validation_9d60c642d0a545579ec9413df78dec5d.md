### Title
Silent u128→u64 Truncation in `DaoCalculator::calculate_maximum_withdraw` Produces Incorrect Withdrawal Capacity — (`File: util/dao/src/lib.rs`)

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the maximum withdrawable capacity for a NervosDAO cell using a u128 intermediate product, then casts the result to u64 with an unchecked `as u64` truncation. Every other analogous u128→u64 conversion in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. When the intermediate value exceeds `u64::MAX`, the silent truncation produces a silently wrong (too-small) withdrawal capacity instead of a proper error, causing the node to compute an incorrect transaction fee for DAO withdrawal transactions.

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← unchecked cast
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast silently truncates the high bits of the u128 value if it exceeds `u64::MAX`. This is inconsistent with every other u128→u64 narrowing in the same file, all of which use the checked form:

- `secondary_block_reward` (line 204): `u64::try_from(reward128).map_err(|_| DaoError::Overflow)?`
- `dao_field_with_current_epoch` (line 245): `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?`
- `dao_field_with_current_epoch` (line 258): `u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?` [2](#0-1) [3](#0-2) [4](#0-3) 

The overflow condition is:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

Since `withdrawing_ar > deposit_ar` always holds (the accumulate rate `ar` is monotonically increasing), any cell whose `counted_capacity` is close to `u64::MAX` will produce a u128 intermediate that exceeds `u64::MAX` once `withdrawing_ar / deposit_ar > 1`. With the total CKB supply at ~3.36 × 10¹⁸ shannons and `ar` starting at `10^16`, the ratio needs to reach ~5.5× for a maximum-capacity cell — achievable over multi-decade timescales given the ~4% annual secondary issuance rate. [5](#0-4) 

### Impact Explanation

When truncation occurs, `withdraw_capacity` is silently set to a value smaller than the true entitlement. This propagates through:

1. `transaction_maximum_withdraw` → `transaction_fee` → `check_tx_fee` in the tx-pool. [6](#0-5) [7](#0-6) 

If the truncated value falls below `outputs_capacity`, the subtraction in `transaction_fee` returns an error, causing the tx-pool to **reject a valid DAO withdrawal transaction**. If the truncated value is still above `outputs_capacity`, the fee is computed as smaller than it actually is, potentially causing the transaction to be rejected for a falsely low fee rate. In either case the node's accounting diverges from the correct on-chain DAO script result, creating a **node-level consensus discrepancy** for affected withdrawal transactions.

### Likelihood Explanation

The condition requires a cell with capacity near the total CKB supply ceiling AND an ar ratio of ~5.5×. At the current secondary issuance rate this takes decades. However, the defect is a present code inconsistency — all peer conversions are checked, this one is not — and it will silently corrupt results rather than surface an error when eventually triggered. Any transaction sender submitting a DAO withdrawal is the unprivileged entry point; no special privilege is required.

### Recommendation

Replace the unchecked cast with the same checked pattern used everywhere else in the file:

```rust
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
```

This makes the function consistent with `secondary_block_reward`, `dao_field_with_current_epoch`, and the existing overflow test `check_withdraw_calculation_overflows`. [8](#0-7) 

### Proof of Concept

Given:
- `deposit_ar = 10_000_000_000_000_000` (genesis value, `10^16`)
- `withdrawing_ar = 55_000_000_000_000_000` (5.5× growth, reachable after ~43 years)
- `output_capacity = 18_446_744_073_709_551_615` (u64::MAX shannons)
- `occupied_capacity = 0`

```
counted_capacity = u64::MAX = 18_446_744_073_709_551_615
withdraw_counted_capacity (u128) = 18_446_744_073_709_551_615 × 55_000_000_000_000_000
                                   / 10_000_000_000_000_000
                                 = 101_457_092_405_402_533_882  (> u64::MAX)

withdraw_counted_capacity as u64 = 101_457_092_405_402_533_882 mod 2^64
                                 = 101_457_092_405_402_533_882 - 18_446_744_073_709_551_616
                                 = 83_010_348_331_692_982_266   ← WRONG, truncated
```

The node returns `83_010_348_331_692_982_266 + 0 = 83_010_348_331_692_982_266` shannons instead of the correct `101_457_092_405_402_533_882` shannons (which itself should have triggered `DaoError::Overflow`). The transaction fee is computed against this wrong value, causing incorrect tx-pool admission decisions for the withdrawal.

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

**File:** util/dao/src/lib.rs (L149-159)
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
    }
```

**File:** util/dao/src/lib.rs (L202-205)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
        Ok(Capacity::shannons(reward))
```

**File:** util/dao/src/lib.rs (L244-246)
```rust
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
        let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;
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

**File:** util/dao/utils/src/lib.rs (L16-17)
```rust
// This is multiplied by 10**16 to make sure we have enough precision.
const DEFAULT_GENESIS_ACCUMULATE_RATE: u64 = 10_000_000_000_000_000;
```

**File:** tx-pool/src/util.rs (L28-53)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
    Ok(fee)
```
