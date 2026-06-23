### Title
`FeeRate::fee()` Rounds Down Instead of Up, Allowing Sub-Minimum-Fee-Rate Transactions to Pass Admission and RBF Checks - (File: util/types/src/core/fee_rate.rs)

---

### Summary

`FeeRate::fee()` computes the minimum fee required for a transaction of a given weight using integer division that truncates (rounds down). When this function is used to enforce the minimum fee rate threshold and the minimum RBF replacement fee, the protocol should round **up** to protect itself — but it rounds down. This allows any tx-pool submitter to craft transactions whose effective fee rate is strictly below the configured `min_fee_rate` or `min_rbf_rate`, yet still pass admission.

---

### Finding Description

`FeeRate::fee()` is defined as:

```rust
// util/types/src/core/fee_rate.rs, line 34-37
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;   // KW = 1000
    Capacity::shannons(fee)
}
```

The division `self.0 * weight / 1000` truncates toward zero. This function is called in two enforcement paths:

**Path 1 — tx-pool admission (`check_tx_fee`):**

```rust
// tx-pool/src/util.rs, line 45-47
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

**Path 2 — RBF minimum replacement fee (`calculate_min_replace_fee`):**

```rust
// tx-pool/src/pool.rs, line 103
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```

In both cases, `fee()` is computing "the minimum amount the submitter must pay." Per the same rounding principle as ERC-4626 — when computing how much a user must provide to meet a threshold, the result must round **up** to protect the protocol. Rounding down produces a `min_fee` that is 1 shannon too low whenever `min_fee_rate * weight` is not exactly divisible by 1000.

**Concrete example:**
- `min_fee_rate = 1001 shannons/KW`, `tx_size = 242 bytes`
- Correct minimum: `ceil(1001 * 242 / 1000)` = `ceil(242.242)` = **243 shannons**
- Computed minimum: `floor(1001 * 242 / 1000)` = `floor(242.242)` = **242 shannons**
- A transaction paying exactly 242 shannons passes, despite its effective fee rate (242/242 = 1000 shannons/KW) being below the configured 1001 shannons/KW threshold.

The same off-by-one applies to `calculate_min_replace_fee`: the `extra_rbf_fee` component is 1 shannon too low, so a replacement transaction can pass the RBF fee check while paying 1 shannon less than the protocol intends.

---

### Impact Explanation

Any unprivileged RPC caller submitting via `send_transaction` can craft a transaction whose fee rate is just below the configured `min_fee_rate` (or `min_rbf_rate`) and have it accepted into the tx-pool. In the RBF case, a replacement transaction can displace an existing transaction while paying 1 shannon less than the protocol-mandated minimum replacement fee. While the per-transaction discrepancy is 1 shannon (10⁻⁸ CKB), the rounding direction is systematically wrong and violates the protocol's stated fee floor invariant. Nodes with non-round `min_fee_rate` values (e.g., 1001, 1500, 1999 shannons/KW) are affected on every transaction whose size does not produce an exact multiple.

---

### Likelihood Explanation

The entry path is fully open: any user submitting a transaction via the `send_transaction` or `send_test_transaction` RPC endpoint triggers `check_tx_fee`, which calls `FeeRate::fee()`. The default `min_fee_rate` is 1000 shannons/KW and `min_rbf_rate` is 1500 shannons/KW. For `min_rbf_rate = 1500` and a transaction of size 241 bytes: `1500 * 241 / 1000 = 361` (exact), no rounding. But for size 243: `1500 * 243 / 1000 = 364` (exact). For size 242: `1500 * 242 / 1000 = 363` (exact). The default rates happen to produce many exact multiples, but any operator-configured non-round rate (e.g., 1001, 1499, 1501) will trigger the rounding discrepancy on most transaction sizes.

---

### Recommendation

Change `FeeRate::fee()` to use ceiling division so that the computed minimum fee always rounds up, protecting the protocol's fee floor:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    // Round up: ceil(rate * weight / KW)
    let fee = (self.0.saturating_mul(weight) + KW - 1) / KW;
    Capacity::shannons(fee)
}
```

This ensures that a transaction must pay at least the true minimum fee for its weight, consistent with the principle that when computing "how much must the user provide," the result should favor the protocol.

---

### Proof of Concept

1. Configure a node with `min_fee_rate = 1001 shannons/KW`.
2. Construct a transaction of serialized size 242 bytes with fee = 242 shannons.
   - Effective fee rate: `242 * 1000 / 242 = 1000 shannons/KW` (below the 1001 minimum).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = FeeRate(1001).fee(242) = 1001 * 242 / 1000 = 242` (truncated).
5. The check `fee < min_fee` evaluates as `242 < 242` → false → transaction is **accepted**.
6. With correct ceiling division, `min_fee = ceil(242.242) = 243`, and the check `242 < 243` → true → transaction is **correctly rejected**. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** util/types/src/core/fee_rate.rs (L33-37)
```rust
    /// Calculates the fee for a given weight.
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** tx-pool/src/util.rs (L44-52)
```rust
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
```

**File:** tx-pool/src/pool.rs (L101-114)
```rust
    /// min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
        // don't account for duplicate txs
        let replaced_fees: HashMap<_, _> = conflicts
            .iter()
            .map(|c| (c.id.clone(), c.inner.fee))
            .collect();
        let replaced_sum_fee = replaced_fees
            .values()
            .try_fold(Capacity::zero(), |acc, x| acc.safe_add(*x));
        let res = replaced_sum_fee.map_or(Err(CapacityError::Overflow), |sum| {
            sum.safe_add(extra_rbf_fee)
        });
```
