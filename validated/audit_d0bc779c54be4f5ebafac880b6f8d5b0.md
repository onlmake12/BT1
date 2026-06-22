### Title
Integer Division Truncation in `FeeRate::fee()` Allows Zero-Fee Transaction Admission When `min_fee_rate` Is Set Below Threshold - (File: `util/types/src/core/fee_rate.rs`)

---

### Summary

The `FeeRate::fee()` function computes the minimum required fee using integer division that truncates (rounds down). When `min_fee_rate * tx_size < 1000`, the computed `min_fee` rounds to 0 shannons. The `check_tx_fee()` function in `tx-pool/src/util.rs` uses this rounded value as the admission threshold, so any transaction — including zero-fee ones — passes the check when `min_fee` evaluates to 0.

---

### Finding Description

In `util/types/src/core/fee_rate.rs`, the `FeeRate::fee()` method computes the minimum fee for a given transaction weight as:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;  // KW = 1000
    Capacity::shannons(fee)
}
``` [1](#0-0) 

The division by `KW = 1000` is integer (floor) division. When `min_fee_rate * tx_size < 1000`, the result truncates to 0.

In `tx-pool/src/util.rs`, `check_tx_fee()` uses this computed value as the admission gate:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(reject);
}
``` [2](#0-1) 

When `min_fee` evaluates to 0, the condition `fee < 0` is never true for an unsigned `Capacity`, so all transactions — including those with zero fee — pass the minimum fee check unconditionally.

The `min_fee_rate` is a configurable parameter: [3](#0-2) 

The default value is 1000 shannons/KW: [4](#0-3) 

With the default rate of 1000, `min_fee = 1000 * tx_size / 1000 = tx_size`, which is always ≥ 1 for any real transaction. However, if an operator sets `min_fee_rate` to a value less than `1000 / tx_size` (e.g., `min_fee_rate = 9` for a 100-byte transaction), the computed `min_fee` rounds to 0.

The `check_tx_fee()` function is called during transaction pre-check before pool admission: [5](#0-4) 

---

### Impact Explanation

A node operator who configures `min_fee_rate` to a small non-zero value (e.g., 9 shannons/KW) intends to enforce a minimum fee, but due to integer truncation, the effective minimum fee is 0 for transactions smaller than `1000 / min_fee_rate` bytes. An attacker who discovers this can submit unlimited zero-fee transactions that bypass the intended admission filter, filling the mempool without paying any fees. This constitutes a tx-pool resource exhaustion / spam vector.

---

### Likelihood Explanation

The default `min_fee_rate = 1000` shannons/KW is safe for all realistic transaction sizes. The vulnerability only manifests when an operator sets `min_fee_rate` below approximately `1000 / min_tx_size`. Since the minimum serialized CKB transaction is on the order of 100+ bytes, any `min_fee_rate < 10` triggers the rounding-to-zero condition. Operators who set very low (but non-zero) fee rates to accommodate low-fee use cases may not be aware that the rounding silently makes the threshold zero. Likelihood is **low** but non-negligible for nodes with custom low-fee configurations.

---

### Recommendation

**Short term**: Use ceiling division in `FeeRate::fee()` to ensure `min_fee ≥ 1` whenever `min_fee_rate > 0` and `weight > 0`:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    let fee = (self.0.saturating_mul(weight) + KW - 1) / KW;
    Capacity::shannons(fee)
}
```

Alternatively, add a post-check: if `min_fee_rate > 0` and the computed `min_fee == 0`, clamp it to 1 shannon.

**Long term**: Add unit tests covering the case where `min_fee_rate * tx_size < 1000` to document and enforce the expected behavior. Consider documenting the minimum effective `min_fee_rate` for typical transaction sizes.

---

### Proof of Concept

1. Configure a CKB node with `min_fee_rate = 9` (9 shannons/KW) in `ckb.toml`.
2. Craft a transaction of serialized size ~100 bytes with zero fee (outputs capacity = inputs capacity).
3. Submit via `send_transaction` RPC.
4. Inside `check_tx_fee()`: `min_fee = FeeRate(9).fee(100) = 9 * 100 / 1000 = 0`.
5. The check `fee < min_fee` becomes `0 < 0` → `false` → transaction is admitted.
6. Repeat indefinitely to fill the mempool with zero-fee transactions at no cost. [1](#0-0) [6](#0-5)

### Citations

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** tx-pool/src/util.rs (L28-54)
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
}
```

**File:** util/app-config/src/configs/tx_pool.rs (L14-16)
```rust
    /// txs with lower fee rate than this will not be relayed or be mined
    #[serde(with = "FeeRateDef")]
    pub min_fee_rate: FeeRate,
```

**File:** util/app-config/src/legacy/tx_pool.rs (L9-10)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
```

**File:** tx-pool/src/process.rs (L289-290)
```rust
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
```
