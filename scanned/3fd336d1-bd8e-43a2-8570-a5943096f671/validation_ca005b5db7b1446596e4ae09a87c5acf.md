### Title
Silent Precision Loss via `saturating_mul` in `FeeRate::calculate` and `FeeRate::fee` Corrupts Tx-Pool Fee Ordering and Admission — (File: `util/types/src/core/fee_rate.rs`)

---

### Summary

`FeeRate::calculate` and `FeeRate::fee` both use `saturating_mul` for an intermediate multiplication. When the product overflows `u64`, the result is silently clamped to `u64::MAX` rather than returning an error or widening to `u128`. This produces a silently incorrect (inflated) fee rate or minimum-fee value, corrupting tx-pool ordering, eviction priority, and the minimum-fee admission gate — all reachable by any tx-pool submitter.

---

### Finding Description

In `util/types/src/core/fee_rate.rs`, both public methods perform a multiplication whose intermediate result can silently overflow:

```rust
// FeeRate::calculate — computes fee rate from a fee and weight
pub fn calculate(fee: Capacity, weight: u64) -> Self {
    if weight == 0 {
        return FeeRate::zero();
    }
    FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)  // KW = 1000
}

// FeeRate::fee — computes the minimum fee for a given weight
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;
    Capacity::shannons(fee)
}
``` [1](#0-0) [2](#0-1) 

`saturating_mul` clamps the product to `u64::MAX` on overflow instead of propagating an error or using wider arithmetic. The subsequent integer division then produces a value derived from `u64::MAX` rather than the mathematically correct result.

**Path 1 — `FeeRate::calculate` inflates fee rate:**

`TxEntry::fee_rate()` calls `FeeRate::calculate(self.fee, weight)` to compute a transaction's fee rate for pool ordering and eviction: [3](#0-2) 

`EvictKey` also uses `FeeRate::calculate` for both the per-entry and descendants fee rate: [4](#0-3) 

If `fee.as_u64() * 1000 > u64::MAX` (i.e., fee > ~18.4 × 10¹⁵ shannons ≈ 184 million CKB), `saturating_mul` returns `u64::MAX`, and the computed fee rate becomes `u64::MAX / weight` — an astronomically inflated value — instead of the correct rate.

**Path 2 — `FeeRate::fee` inflates minimum-fee admission gate:**

`check_tx_fee` in the tx-pool uses `FeeRate::fee` to compute the minimum fee a transaction must pay: [5](#0-4) 

If `min_fee_rate * tx_size > u64::MAX`, `saturating_mul` returns `u64::MAX`, and `min_fee` becomes `u64::MAX / 1000 ≈ 1.844 × 10¹⁶ shannons` — effectively blocking all transactions from admission.

The contrast with the rest of the codebase is stark: every other arithmetic operation in the DAO and capacity subsystems uses `checked_add`, `safe_add`, `safe_sub`, or explicit `u128` widening to avoid silent precision loss: [6](#0-5) [7](#0-6) 

`FeeRate` is the only production arithmetic site that uses `saturating_mul` without any guard.

---

### Impact Explanation

- **Tx-pool ordering corruption**: A transaction whose fee causes overflow in `FeeRate::calculate` receives an inflated fee rate (`u64::MAX / weight`), placing it at the top of the ordering queue regardless of its actual economic priority. This distorts miner block-template selection.
- **Eviction priority corruption**: The `EvictKey` uses the same inflated fee rate, making the affected transaction immune to eviction even when the pool is full.
- **Admission gate denial**: If `min_fee_rate` is set to a value where `min_fee_rate * tx_size` overflows (e.g., via RPC or config), `FeeRate::fee` returns a silently inflated minimum fee, causing all transactions below that threshold to be incorrectly rejected with `LowFeeRate`.

---

### Likelihood Explanation

- **Path 1** requires a transaction fee exceeding ~184 million CKB. While the total CKB supply (~33.6 billion CKB) makes this theoretically possible, it is economically irrational in normal operation. However, the code contains no guard, and the behavior is silent — no error is returned, no log is emitted.
- **Path 2** is reachable with a high `min_fee_rate` configuration combined with large transactions. `FeeRate` is a `u64` with no upper bound enforced at construction (`from_u64` is unchecked), so any value up to `u64::MAX` is accepted. A node operator or RPC caller setting an extreme fee rate triggers the silent inflation.

---

### Recommendation

Replace `saturating_mul` with either:
1. Checked multiplication returning an error on overflow, consistent with the rest of the codebase (`checked_mul`).
2. Widening to `u128` for the intermediate product before dividing and then range-checking the result back to `u64`, matching the pattern used in `DaoCalculator::calculate_maximum_withdraw` and `dao_field_with_current_epoch`.

```rust
// Example fix for FeeRate::calculate
pub fn calculate(fee: Capacity, weight: u64) -> Self {
    if weight == 0 {
        return FeeRate::zero();
    }
    let fee_u128 = u128::from(fee.as_u64());
    let kw_u128 = u128::from(KW);
    let weight_u128 = u128::from(weight);
    let rate = (fee_u128 * kw_u128 / weight_u128).min(u128::from(u64::MAX));
    FeeRate::from_u64(rate as u64)
}
```

---

### Proof of Concept

1. Construct a transaction where `inputs_capacity - outputs_capacity > u64::MAX / 1000` shannons (fee > ~184 million CKB). This is valid at the type level since `Capacity` is a plain `u64`.
2. Submit it to the tx-pool via `send_transaction` RPC.
3. `check_tx_fee` computes the fee correctly via `DaoCalculator::transaction_fee`.
4. `TxEntry::fee_rate()` calls `FeeRate::calculate(fee, weight)`. Since `fee.as_u64() * 1000 > u64::MAX`, `saturating_mul` returns `u64::MAX`, and the fee rate is computed as `u64::MAX / weight` — orders of magnitude higher than the true rate.
5. The transaction is inserted at the top of the pool's ordering queue and is immune to eviction, regardless of competing transactions with legitimately higher fee rates. [8](#0-7) [3](#0-2) [9](#0-8)

### Citations

**File:** util/types/src/core/fee_rate.rs (L1-38)
```rust
use crate::core::Capacity;

/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;

impl FeeRate {
    /// Calculates the fee rate from a total fee and weight.
    pub fn calculate(fee: Capacity, weight: u64) -> Self {
        if weight == 0 {
            return FeeRate::zero();
        }
        FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
    }

    /// Creates a fee rate from shannons per kilo-weight.
    pub const fn from_u64(fee_per_kw: u64) -> Self {
        FeeRate(fee_per_kw)
    }

    /// Returns the fee rate as shannons per kilo-weight.
    pub const fn as_u64(self) -> u64 {
        self.0
    }

    /// Creates a zero fee rate.
    pub const fn zero() -> Self {
        Self::from_u64(0)
    }

    /// Calculates the fee for a given weight.
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
}
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/component/entry.rs (L234-247)
```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);

        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            timestamp: entry.timestamp,
            descendants_count: entry.descendants_count,
        }
    }
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

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/occupied-capacity/core/src/units.rs (L124-138)
```rust
    /// Adds self and rhs and checks overflow error.
    pub fn safe_add<C: IntoCapacity>(self, rhs: C) -> Result<Self> {
        self.0
            .checked_add(rhs.into_capacity().0)
            .map(Capacity::shannons)
            .ok_or(Error::Overflow)
    }

    /// Subtracts self and rhs and checks overflow error.
    pub fn safe_sub<C: IntoCapacity>(self, rhs: C) -> Result<Self> {
        self.0
            .checked_sub(rhs.into_capacity().0)
            .map(Capacity::shannons)
            .ok_or(Error::Overflow)
    }
```
