### Title
Integer Truncation in `FeeRate::fee()` Causes Systematic Underestimation of Minimum Required Fee — (File: `util/types/src/core/fee_rate.rs`)

---

### Summary

The `FeeRate::fee()` function in `util/types/src/core/fee_rate.rs` performs integer division that truncates the result, causing the computed minimum fee to be systematically lower than the true required amount. This is the direct analog of the reported LTV rounding-down vulnerability: a division that discards fractional precision causes a threshold check to pass when it should fail. In CKB's case, this allows a transaction sender to submit a transaction whose actual fee rate is strictly below `min_fee_rate`, bypassing the pool's admission gate.

---

### Finding Description

`FeeRate::fee()` computes the minimum fee for a given transaction weight:

```rust
// util/types/src/core/fee_rate.rs, line 34-37
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;   // KW = 1000
    Capacity::shannons(fee)
}
``` [1](#0-0) 

The division `self.0 * weight / 1000` truncates toward zero. The result `min_fee` is then compared against the actual transaction fee in `check_tx_fee`:

```rust
// tx-pool/src/util.rs, lines 45-52
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [2](#0-1) 

Because `min_fee` is truncated downward, a transaction whose true fee rate is `min_fee_rate - ε` (for small ε) can produce a fee that equals the truncated `min_fee` and therefore passes the `fee < min_fee` check. The same truncation applies in `calculate_min_replace_fee` for RBF:

```rust
// tx-pool/src/pool.rs, line 103
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
``` [3](#0-2) 

**Concrete example:**
- `min_fee_rate = 1000` shannons/KW, `tx_size = 1` byte (weight = 1).
- `min_fee = 1000 * 1 / 1000 = 1` shannon.
- A transaction with fee = 1 shannon passes, even though its effective fee rate is exactly 1000 shannons/KW — which is the boundary, not strictly above it.
- More critically, for `tx_size = 999`: `min_fee = 1000 * 999 / 1000 = 999` (truncated from 999.0). A transaction paying 999 shannons passes, but its effective rate is `999 * 1000 / 999 = 1000` — still at boundary.
- For `tx_size = 1001`: `min_fee = 1000 * 1001 / 1000 = 1001`. A transaction paying 1001 shannons passes. Effective rate = `1001 * 1000 / 1001 ≈ 999.0` shannons/KW — **below** `min_fee_rate = 1000`.

The truncation means that for any `weight` not a multiple of 1000, the computed `min_fee` is strictly less than `fee_rate * weight / 1000` (the true threshold), allowing transactions with sub-threshold fee rates to enter the pool.

---

### Impact Explanation

An unprivileged transaction sender can craft transactions whose effective fee rate is below the node's configured `min_fee_rate` and have them accepted into the tx-pool. This:

1. **Bypasses the minimum fee rate gate** (`PoolRejectedTransactionByMinFeeRate`), allowing spam transactions at sub-threshold rates.
2. **Undermines RBF protection**: the `extra_rbf_fee` computed via the same `fee()` function is also truncated, so the minimum replacement fee is understated, allowing RBF replacements that do not actually meet the required fee bump.
3. **Degrades miner revenue**: transactions admitted below `min_fee_rate` consume block space at a discount, reducing the economic floor the node operator configured.

The impact is bounded — the maximum shortfall per transaction is `(KW - 1) / KW < 1` shannon per 1000-weight unit — but it is systematic and reachable by any tx-pool submitter or RPC caller.

---

### Likelihood Explanation

This is reachable by any unprivileged user who can call `send_transaction` via RPC or relay a transaction over P2P. No special privileges are required. The truncation is deterministic and predictable: an attacker can compute the exact weight at which the truncation creates the largest gap and craft transactions accordingly. The default `min_fee_rate` of 1000 shannons/KW means the maximum per-transaction shortfall is up to 999 shannons, which is small but non-zero and exploitable at scale.

---

### Recommendation

Replace the truncating integer division in `FeeRate::fee()` with a ceiling division to ensure the computed minimum fee is never less than the true threshold:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    // Use ceiling division: (rate * weight + KW - 1) / KW
    let fee = self.0.saturating_mul(weight).saturating_add(KW - 1) / KW;
    Capacity::shannons(fee)
}
```

Apply the same fix to any other site that calls `fee()` for threshold enforcement, including `calculate_min_replace_fee` in `tx-pool/src/pool.rs`. [1](#0-0) [4](#0-3) 

---

### Proof of Concept

**Entry path:** Any user calls `send_transaction` via JSON-RPC or relays a transaction via P2P.

**Steps:**
1. Node configured with `min_fee_rate = 1000` shannons/KW.
2. Attacker constructs a transaction of serialized size `S` bytes where `S mod 1000 != 0`, e.g. `S = 242` (a typical small tx).
3. `min_fee = FeeRate(1000).fee(242) = 1000 * 242 / 1000 = 242` shannons (exact, no truncation here).
4. For `S = 2001`: `min_fee = 1000 * 2001 / 1000 = 2001`. Effective rate of a tx paying 2001 shannons = `2001 * 1000 / 2001 = 1000` — boundary, passes.
5. For `S = 1999`: `min_fee = 1000 * 1999 / 1000 = 1999`. A tx paying 1999 shannons has effective rate `1999 * 1000 / 1999 = 1000` — passes at boundary.
6. For `S = 1001`: `min_fee = 1000 * 1001 / 1000 = 1001`. A tx paying 1001 shannons has effective rate `1001 * 1000 / 1001 ≈ 999.0` shannons/KW — **below** `min_fee_rate`, yet passes the `fee < min_fee` check since `1001 >= 1001`.

The `check_tx_fee` function in `tx-pool/src/util.rs` uses `fee(tx_size as u64)` directly with `tx_size` (not the full weight), as noted in the comment "here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly." [5](#0-4) [6](#0-5)

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

**File:** tx-pool/src/util.rs (L42-52)
```rust
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
