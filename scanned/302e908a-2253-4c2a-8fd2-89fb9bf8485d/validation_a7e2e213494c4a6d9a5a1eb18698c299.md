### Title
`FeeRate::fee()` Integer Division Rounds `min_fee` to Zero, Bypassing Minimum Fee Rate Enforcement — (`util/types/src/core/fee_rate.rs`)

---

### Summary

`FeeRate::fee()` computes the minimum required fee via integer division `(rate * weight) / 1000`. When `rate * tx_size < 1000`, the result truncates to zero. In `check_tx_fee`, this zero `min_fee` causes the guard `if fee < min_fee` to pass for any transaction — including zero-fee ones — even when `min_fee_rate > 0`. The same truncation affects `calculate_min_replace_fee`, where `extra_rbf_fee` can silently become zero, eliminating the mandatory fee bump for RBF replacements.

---

### Finding Description

`FeeRate::fee()` is defined as:

```rust
// util/types/src/core/fee_rate.rs, line 34-37
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;  // KW = 1000
    Capacity::shannons(fee)
}
```

Integer division truncates toward zero. When `self.0 * weight < 1000`, the result is `0`.

This value is used directly as the admission threshold in `check_tx_fee`:

```rust
// tx-pool/src/util.rs, line 45-52
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {   // if min_fee == 0, this is never true
    return Err(Reject::LowFeeRate(...));
}
```

And as the mandatory extra-fee component for RBF in `calculate_min_replace_fee`:

```rust
// tx-pool/src/pool.rs, line 103
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
// min_replace_fee = sum(replaced_fees) + extra_rbf_fee
// if extra_rbf_fee == 0, no bump is required
```

---

### Impact Explanation

**Tx-pool admission bypass**: Any RPC caller (`send_transaction`) or relay peer can submit a zero-fee transaction to a node whose `min_fee_rate` is configured to a value between 1 and 999 shannons/KW. For such a node, any transaction whose `min_fee_rate * tx_size_bytes < 1000` will compute `min_fee = 0`, and the zero-fee transaction passes the `fee < min_fee` guard unconditionally.

**RBF extra-fee bypass**: When `min_rbf_rate * tx_size < 1000`, `extra_rbf_fee = 0`. The replacement transaction only needs to match the aggregate fee of the replaced transactions, with no incremental bump. This undermines the anti-spam purpose of the RBF fee increment rule.

Neither case panics or errors — the zero result is silently accepted as a valid threshold.

---

### Likelihood Explanation

The default `min_fee_rate` is 1000 shannons/KW. At exactly 1000, `1000 * N / 1000 = N` for any integer `N`, so no rounding occurs at the default. However:

- The configuration field accepts any `u64` value; a node operator setting `min_fee_rate` to any value in `[1, 999]` (e.g., to allow cheaper transactions) is immediately vulnerable.
- The relay test suite itself demonstrates nodes configured with `min_fee_rate = 0` and `min_fee_rate = 1000`, showing the range is intentionally variable.
- An unprivileged tx-pool submitter (RPC caller or relay peer) needs only to craft a small transaction (< `1000 / min_fee_rate` bytes) with zero fee. Minimum CKB transaction sizes are well under 1000 bytes (the relay test shows 242 bytes for a typical transaction).

---

### Recommendation

Add a zero-guard in `check_tx_fee` analogous to the M-12 fix: if `min_fee_rate > 0` but the computed `min_fee` rounds to zero, clamp it to 1 shannon. Alternatively, apply the same guard inside `FeeRate::fee()` itself:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    if self.0 == 0 {
        return Capacity::zero();
    }
    let fee = self.0.saturating_mul(weight) / KW;
    // Ensure a non-zero rate always produces at least 1 shannon
    Capacity::shannons(fee.max(1))
}
```

Or, in `check_tx_fee`:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
let min_fee = if tx_pool.config.min_fee_rate > FeeRate::zero() && min_fee.is_zero() {
    Capacity::one()
} else {
    min_fee
};
```

Apply the same fix to `calculate_min_replace_fee` for `extra_rbf_fee`.

---

### Proof of Concept

**Configuration**: `min_fee_rate = 1` (1 shannon/KW — a valid non-zero value).

**Transaction**: any valid CKB transaction of size 999 bytes with zero fee (outputs capacity == inputs capacity).

**Trace**:

```
min_fee = FeeRate(1).fee(999)
        = (1 * 999) / 1000
        = 0          ← truncated
fee     = 0          ← zero-fee transaction

check: fee < min_fee  →  0 < 0  →  false  →  transaction ACCEPTED
```

The zero-fee transaction bypasses `PoolRejectedTransactionByMinFeeRate` and enters the pool, even though `min_fee_rate > 0` was explicitly configured.

**Root cause lines**: [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

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
