### Title
Rounding-Down in `FeeRate::fee()` Allows Transactions to Bypass Minimum Fee Rate Admission Check - (`util/types/src/core/fee_rate.rs`)

---

### Summary

`FeeRate::fee()` computes the minimum fee required for a given transaction weight using integer floor division. Because the result is rounded down, a transaction submitter can craft a transaction whose actual fee rate is strictly below the node's configured `min_fee_rate`, yet still pass the tx-pool admission check in `check_tx_fee()`. This is the direct CKB analog of the RubiconMarket `spend` rounding-down bug: in both cases, the "amount owed" is floored, letting the payer systematically underpay.

---

### Finding Description

`FeeRate::fee()` computes the minimum fee for a transaction of a given weight:

```rust
// util/types/src/core/fee_rate.rs, line 34-37
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;   // KW = 1000, integer floor division
    Capacity::shannons(fee)
}
``` [1](#0-0) 

This result is used directly as the threshold in the tx-pool admission gate:

```rust
// tx-pool/src/util.rs, line 45-52
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    let reject = Reject::LowFeeRate(...);
    return Err(reject);
}
``` [2](#0-1) 

Because `fee_rate * weight` is not always divisible by 1000, the floor truncates the required minimum by up to 1 shannon. A submitter who pays exactly `floor(fee_rate * weight / 1000)` shannons passes the check even though their actual effective fee rate — `paid_fee * 1000 / weight` — is strictly less than `min_fee_rate`.

**Concrete example** (non-default `min_fee_rate = 1001` shannons/KW, `tx_size = 999` bytes):

| Value | Calculation | Result |
|---|---|---|
| `min_fee` (floored) | `floor(1001 × 999 / 1000)` | **999 shannons** |
| Correct minimum (ceiling) | `ceil(1001 × 999 / 1000)` | **1000 shannons** |
| Effective fee rate of tx paying 999 | `999 × 1000 / 999` | **1000 shannons/KW** |

A transaction paying 999 shannons passes `fee >= min_fee` (999 ≥ 999), yet its effective fee rate is 1000 shannons/KW — below the configured threshold of 1001.

The same floor division appears in `FeeRate::calculate()`:

```rust
FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
``` [3](#0-2) 

---

### Impact Explanation

Any unprivileged RPC caller submitting transactions via `send_transaction` can systematically underpay fees by up to 1 shannon per transaction relative to the node's `min_fee_rate` policy. Across many transactions this:

1. **Weakens spam-prevention**: The `min_fee_rate` guard is the primary economic barrier against tx-pool flooding. Bypassing it — even by 1 shannon — means the effective floor is `min_fee_rate - 1` for certain transaction sizes, undermining the node operator's configured policy.
2. **Enables fee-rate arbitrage at scale**: A submitter who knows which `(fee_rate, size)` pairs trigger the rounding can submit large volumes of transactions at a fee rate below the advertised minimum, gaining a systematic cost advantage over honest submitters.

The per-transaction "profit" is bounded by 1 shannon (the smallest CKB unit), but the effect is deterministic and repeatable for any `(fee_rate × size) mod 1000 ≠ 0` combination.

---

### Likelihood Explanation

- **Entry path**: Any node reachable via RPC. No authentication, no special role, no hashpower required.
- **Triggering condition**: Occurs whenever `min_fee_rate × tx_size` is not a multiple of 1000. With the default `min_fee_rate = 1000` shannons/KW and `KW = 1000`, the division is exact for all sizes, so the default configuration is not affected. However, any operator who sets a non-round `min_fee_rate` (e.g., 1001, 1500, or any value not a multiple of 1000) is exposed.
- **Detectability**: The rounding is silent — no log, no error. The submitter simply pays the floored amount and the transaction is accepted. [4](#0-3) 

---

### Recommendation

Replace the floor division in `FeeRate::fee()` with ceiling division so that the minimum fee required is always at least `fee_rate * weight / 1000`, rounded up:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    // Ceiling division: (a + b - 1) / b
    let fee = (self.0.saturating_mul(weight) + KW - 1) / KW;
    Capacity::shannons(fee)
}
```

This ensures that a transaction paying exactly `min_fee` always has an effective fee rate ≥ `min_fee_rate`, closing the systematic underpayment gap.

---

### Proof of Concept

Given `min_fee_rate = 1001` shannons/KW and `tx_size = 999` bytes:

```
min_fee (current, floored)  = floor(1001 * 999 / 1000) = floor(999999 / 1000) = 999 shannons
min_fee (correct, ceiling)  = ceil(1001 * 999 / 1000)  = ceil(999.999)        = 1000 shannons

A transaction with fee = 999 shannons, size = 999 bytes:
  - Passes check_tx_fee(): 999 >= 999  ✓
  - Effective fee rate:    999 * 1000 / 999 = 1000 shannons/KW
  - Configured minimum:    1001 shannons/KW
  - Underpayment:          1 shannon (1 tx), N shannons (N txs)
```

The attacker submits transactions sized to maximize `(fee_rate * size) mod 1000`, paying 1 shannon less per transaction than the node's policy requires. With `min_rbf_rate = 1500` shannons/KW (also non-round), the same attack applies to RBF replacement fee checks via `calculate_min_replace_fee()`. [5](#0-4)

### Citations

**File:** util/types/src/core/fee_rate.rs (L11-16)
```rust
    pub fn calculate(fee: Capacity, weight: u64) -> Self {
        if weight == 0 {
            return FeeRate::zero();
        }
        FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
    }
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** tx-pool/src/util.rs (L45-52)
```rust
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L10-12)
```rust
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
```

**File:** tx-pool/src/pool.rs (L101-115)
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
        if let Ok(res) = res {
```
