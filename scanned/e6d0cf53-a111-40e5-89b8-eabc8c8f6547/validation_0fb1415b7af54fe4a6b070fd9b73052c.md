### Title
Integer Truncation in `FeeRate::fee()` Allows Zero-Fee Transaction Admission When `min_fee_rate < 1000` — (File: `util/types/src/core/fee_rate.rs`)

---

### Summary

`FeeRate::fee(weight)` computes the minimum required fee using integer division `(fee_rate * weight) / 1000`. When `fee_rate * weight < 1000`, the result truncates to **0 shannons**. The tx-pool admission check in `check_tx_fee` then computes `min_fee = 0` and accepts any transaction — including zero-fee ones — because the comparison `fee < min_fee` is `fee < 0`, which is never true for a `u64`. Any node operator who sets `min_fee_rate` to a value between 1 and 999 shannons/KW (a supported, documented configuration) is silently exposed: transactions smaller than `1000 / min_fee_rate` bytes bypass the fee floor entirely and are admitted for free.

---

### Finding Description

`FeeRate::fee()` is defined as:

```rust
// util/types/src/core/fee_rate.rs, line 34-37
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;   // KW = 1000
    Capacity::shannons(fee)
}
``` [1](#0-0) 

`KW` is the constant `1000`. Integer division truncates toward zero, so whenever `self.0 * weight < 1000` the result is `0`.

This value is consumed directly in the tx-pool admission gate:

```rust
// tx-pool/src/util.rs, lines 45-52
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    let reject = Reject::LowFeeRate(...);
    return Err(reject);
}
``` [2](#0-1) 

When `min_fee` evaluates to `Capacity::shannons(0)`, the guard `fee < min_fee` is `fee < 0`, which is impossible for a `u64`. Every transaction — including those with zero fee — passes.

**Concrete example:**

| `min_fee_rate` (shannons/KW) | `tx_size` (bytes) | `min_fee` computed | Zero-fee tx admitted? |
|---|---|---|---|
| 1000 (default) | 242 | 242 shannons | No |
| 1 | 999 | `1 * 999 / 1000 = 0` | **Yes** |
| 500 | 1 | `500 * 1 / 1000 = 0` | **Yes** |
| 999 | 1 | `999 * 1 / 1000 = 0` | **Yes** |

The default `min_fee_rate = 1000` is not affected because `1000 * weight / 1000 = weight` (no truncation for weight ≥ 1). However, any value in `[1, 999]` creates a window of transaction sizes where `min_fee = 0`.

The configuration is explicitly supported and documented:

```toml
# resource/ckb.toml, line 212
min_fee_rate = 1_000  # shannons/KB
``` [3](#0-2) 

---

### Impact Explanation

A transaction sender who knows a target node's `min_fee_rate` is below 1000 can craft a transaction whose serialized size is less than `1000 / min_fee_rate` bytes and submit it with zero fee. The node's `check_tx_fee` gate computes `min_fee = 0` and admits the transaction unconditionally. This:

- Allows unlimited zero-fee transactions to fill the mempool on affected nodes.
- Undermines the operator's explicit intent to enforce a minimum fee floor.
- Can be used to exhaust mempool capacity (`max_tx_pool_size = 180 MB` by default), degrading service for legitimate fee-paying transactions.

The impact is analogous to the external report: a small-value input truncates to zero in an integer division, allowing the sender to obtain a service (mempool admission) for free.

---

### Likelihood Explanation

The default `min_fee_rate = 1000` is not vulnerable. However:

- Operators who lower `min_fee_rate` to attract more transactions (e.g., private/test networks, low-traffic nodes) are silently exposed.
- The configuration is fully supported and there is no documentation warning about the truncation behavior.
- The attacker entry path is trivial: submit a small zero-fee transaction via the `send_transaction` RPC. [4](#0-3) 

---

### Recommendation

Round up instead of truncating in `FeeRate::fee()`. Replace the integer floor division with a ceiling division:

```rust
// util/types/src/core/fee_rate.rs
pub fn fee(self, weight: u64) -> Capacity {
    // Use ceiling division: (a + b - 1) / b
    let fee = self.0.saturating_mul(weight).saturating_add(KW - 1) / KW;
    Capacity::shannons(fee)
}
```

This ensures that any non-zero fee rate applied to any non-zero weight always produces at least 1 shannon, matching the operator's intent. Alternatively, add a post-condition: if `fee_rate > 0 && weight > 0 && computed_fee == 0`, return `Capacity::one()`. [1](#0-0) 

---

### Proof of Concept

1. Start a CKB node with `min_fee_rate = 1` in `ckb.toml`.
2. Construct a valid transaction whose serialized size is 500 bytes (well within the typical range).
3. Set all outputs' total capacity equal to all inputs' total capacity (zero fee).
4. Submit via `send_transaction` RPC.

Expected (correct) behavior: rejected with `LowFeeRate`.

Actual behavior: admitted, because `FeeRate(1).fee(500) = 1 * 500 / 1000 = 0`, so `min_fee = 0 shannons`, and `0 < 0` is false. [5](#0-4) [1](#0-0)

### Citations

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
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

**File:** resource/ckb.toml (L212-214)
```text
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
```

**File:** util/app-config/src/legacy/tx_pool.rs (L9-12)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
```
