### Title
`FeeRate::fee()` Rounds Down Minimum Fee Threshold, Allowing Transactions to Underpay the Configured Fee Rate - (File: `util/types/src/core/fee_rate.rs`)

---

### Summary

`FeeRate::fee()` uses integer floor division to compute the minimum fee for a given transaction weight. This is the direct analog of the ERC-4626 `previewWithdraw` rounding bug: a conversion function that translates a rate into an amount rounds in favor of the submitter (user) rather than the protocol. The result is that any transaction whose weight is not an exact multiple of 1000 can be admitted to the tx-pool while paying a fee rate strictly below the configured `min_fee_rate`.

---

### Finding Description

`FeeRate::fee()` is defined as:

```rust
// util/types/src/core/fee_rate.rs, line 34-37
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;   // KW = 1000, floor division
    Capacity::shannons(fee)
}
``` [1](#0-0) 

The result is `⌊fee_rate × weight / 1000⌋`. This value is used directly as the minimum fee threshold in `check_tx_fee()`:

```rust
// tx-pool/src/util.rs, lines 45-51
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [2](#0-1) 

Because `min_fee` is floored, a transaction paying exactly `min_fee` shannons passes the check even though its true fee rate — `min_fee × 1000 / weight` — is strictly less than `min_fee_rate` whenever `fee_rate × weight` is not divisible by 1000.

The correct behavior, mirroring the EIP-4626 recommendation to round in favor of the vault, would be ceiling division:

```
min_fee = ⌈fee_rate × weight / 1000⌉
        = (fee_rate × weight + 999) / 1000
```

---

### Impact Explanation

Any unprivileged transaction submitter (RPC caller, P2P relay sender) can craft a transaction whose weight is not a multiple of 1000 and pay a fee that is 1–999 shannons below the exact minimum implied by `min_fee_rate`. The maximum per-transaction underpayment is `KW − 1 = 999` shannons (< 0.00001 CKB). Over a sustained volume of transactions this constitutes a slow, continuous value leak from miners: they accept transactions at a fee rate that is measurably below the operator-configured floor, with no way to detect or reject them at the protocol level.

**Concrete example:**
- `min_fee_rate = 1001 shannons/KW`, `weight = 999`
- `min_fee = ⌊1001 × 999 / 1000⌋ = ⌊999 999 / 1000⌋ = 999` shannons
- Actual fee rate paid: `999 × 1000 / 999 = 1000` shannons/KW — 1 unit below the configured floor
- The transaction is accepted

---

### Likelihood Explanation

High. Any transaction whose serialized size (used as weight in the cheap check) is not an exact multiple of 1000 bytes and whose fee rate is not a multiple of 1000 shannons/KW will trigger this rounding. In practice, almost every transaction falls into this category. The entry path is fully unprivileged: any RPC caller or P2P peer can submit such a transaction. [3](#0-2) 

---

### Recommendation

Replace floor division with ceiling division in `FeeRate::fee()`:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    // Round up so the minimum fee always meets or exceeds the configured rate
    let fee = self.0.saturating_mul(weight).saturating_add(KW - 1) / KW;
    Capacity::shannons(fee)
}
``` [1](#0-0) 

This mirrors the EIP-4626 fix: the conversion from rate to amount should round in favor of the protocol (miners / the fee floor), not the submitter.

---

### Proof of Concept

**Setup:** Node configured with `min_fee_rate = 1001 shannons/KW`.

**Craft a transaction** with serialized size `tx_size = 999` bytes and attach a fee of exactly `999` shannons.

**Current behavior:**
```
min_fee = FeeRate(1001).fee(999)
        = 1001 * 999 / 1000          // floor
        = 999999 / 1000
        = 999 shannons
fee (999) < min_fee (999) → false    // check passes
```

The transaction is admitted to the tx-pool. The actual fee rate is `999 * 1000 / 999 = 1000` shannons/KW, which is 1 unit below the operator-configured minimum of 1001 shannons/KW.

**With ceiling division:**
```
min_fee = (1001 * 999 + 999) / 1000
        = 1000998 / 1000
        = 1000 shannons
fee (999) < min_fee (1000) → true    // correctly rejected
``` [4](#0-3) [5](#0-4)

### Citations

**File:** util/types/src/core/fee_rate.rs (L7-37)
```rust
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
