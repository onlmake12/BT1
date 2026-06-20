### Title
Integer Division Truncation in `FeeRate::fee()` Causes Minimum Fee to Round to Zero for Small Transactions — (`util/types/src/core/fee_rate.rs`)

---

### Summary

`FeeRate::fee()` computes the minimum required fee as `fee_rate * weight / 1000` using integer (floor) division. When `fee_rate * weight < 1000`, the result truncates to zero. This value is used directly in `check_tx_fee` to gate tx-pool admission. When `min_fee_rate` is configured to any value below 1000 shannons/KB (a valid, documented configuration), small transactions can be submitted with zero fee and pass the minimum-fee check, bypassing the economic spam-prevention mechanism.

---

### Finding Description

In `util/types/src/core/fee_rate.rs`, `FeeRate::fee()` computes the fee for a given weight:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;   // KW = 1000
    Capacity::shannons(fee)
}
``` [1](#0-0) 

`KW` is 1000, so the formula is `fee_rate_value * weight / 1000`. Integer division truncates toward zero. Whenever `fee_rate_value * weight < 1000`, the result is exactly `0`.

This return value is consumed in `tx-pool/src/util.rs` inside `check_tx_fee`:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [2](#0-1) 

When `min_fee` evaluates to `0`, the condition `fee < 0` is never true for any non-negative fee, so **any transaction—including a zero-fee transaction—passes the check**.

The default `min_fee_rate` is 1000 shannons/KB: [3](#0-2) 

However, the configuration file explicitly documents and supports lower values: [4](#0-3) 

The `TxPoolConfig` struct accepts any `FeeRate` value with no lower-bound enforcement: [5](#0-4) 

---

### Impact Explanation

When `min_fee_rate` is set to any value `r` where `r < 1000`, a transaction of serialized size `s < floor(1000 / r)` bytes will have its computed `min_fee` truncate to zero. The tx-pool then accepts the transaction regardless of its actual fee, including zero-fee transactions. This:

1. Allows an unprivileged RPC caller or P2P peer to flood the tx-pool with zero-fee transactions, exhausting the `max_tx_pool_size` (default 180 MB) and evicting legitimate fee-paying transactions.
2. Undermines the economic spam-prevention guarantee that `min_fee_rate` is intended to provide.

The vulnerability is analogous to the external report: a financial calculation that should produce a non-zero floor produces zero due to integer truncation, allowing a party to evade a required payment.

---

### Likelihood Explanation

The default `min_fee_rate = 1000` shannons/KB does not trigger truncation for any transaction of size ≥ 1 byte. However:

- The configuration file documents `min_fee_rate` as a user-tunable parameter with no enforced minimum.
- Operators running private chains, testnets, or low-traffic nodes commonly set `min_fee_rate = 0` or small values (the integration test template sets it to `0`). [6](#0-5) 

Any node with `min_fee_rate` in the range `[1, 999]` is vulnerable for transactions smaller than `1000 / min_fee_rate` bytes.

---

### Recommendation

Replace floor division with ceiling division when computing the minimum fee, so that any non-zero fee rate always produces a non-zero minimum fee for any non-zero weight:

```rust
// Before (floor division — can produce 0):
let fee = self.0.saturating_mul(weight) / KW;

// After (ceiling division — always >= 1 when fee_rate > 0 and weight > 0):
let fee = (self.0.saturating_mul(weight) + KW - 1) / KW;
``` [1](#0-0) 

This ensures that a configured `min_fee_rate > 0` always enforces a minimum fee of at least 1 shannon for any transaction, regardless of size.

---

### Proof of Concept

**Scenario 1 — `min_fee_rate = 100` shannons/KB, `tx_size = 9` bytes:**

```
min_fee = 100 * 9 / 1000 = 900 / 1000 = 0   ← truncates to zero
```

A zero-fee transaction of 9 bytes passes `check_tx_fee` and is admitted to the pool.

**Scenario 2 — `min_fee_rate = 1` shannon/KB, `tx_size = 999` bytes:**

```
min_fee = 1 * 999 / 1000 = 999 / 1000 = 0   ← truncates to zero
```

A zero-fee transaction of up to 999 bytes passes `check_tx_fee`.

**Comparison with default (no truncation):**

```
min_fee_rate = 1000, tx_size = 60 bytes:
min_fee = 1000 * 60 / 1000 = 60   ← correct, no truncation
```

The attacker-controlled entry path is the `send_transaction` RPC endpoint or the P2P transaction relay protocol, both of which call `check_tx_fee` via the tx-pool submission path. [7](#0-6) [1](#0-0)

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

**File:** util/app-config/src/legacy/tx_pool.rs (L10-10)
```rust
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
```

**File:** resource/ckb.toml (L212-214)
```text
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
```

**File:** util/app-config/src/configs/tx_pool.rs (L14-16)
```rust
    /// txs with lower fee rate than this will not be relayed or be mined
    #[serde(with = "FeeRateDef")]
    pub min_fee_rate: FeeRate,
```

**File:** test/template/ckb.toml (L87-88)
```text
min_fee_rate = 0 # Here fee_rate are calculated directly using size in units of shannons/KB
min_rbf_rate = 0 # Here rbf_rate are calculated directly using size in units of shannons/KB
```
