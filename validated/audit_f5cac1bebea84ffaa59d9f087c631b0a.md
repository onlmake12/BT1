### Title
`FeeRate::fee()` Integer Division Truncation Allows Minimum Fee Check to Produce Zero — (`util/types/src/core/fee_rate.rs`)

---

### Summary

`FeeRate::fee()` computes the minimum required fee using integer (floor) division: `rate * weight / 1000`. When `min_fee_rate * tx_size < 1000`, the result truncates to **zero**. The tx-pool admission gate `check_tx_fee` uses this value directly; a zero `min_fee` means any transaction — including one with a zero fee — passes the check unconditionally, bypassing the configured minimum fee rate entirely.

---

### Finding Description

`FeeRate::fee()` is defined as:

```rust
// util/types/src/core/fee_rate.rs  line 34-37
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;   // KW = 1000, truncates
    Capacity::shannons(fee)
}
``` [1](#0-0) 

`KW` is the constant `1000`. The division is plain integer division — it floors the result. Whenever `min_fee_rate * tx_size < 1000`, the quotient is **0**.

This value is consumed directly in the tx-pool admission check:

```rust
// tx-pool/src/util.rs  line 45-52
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [2](#0-1) 

When `min_fee` evaluates to `0`, the condition `fee < 0` is structurally impossible (fees are unsigned), so **every transaction passes regardless of its actual fee**.

The default `min_fee_rate` is 1000 shannons/KW: [3](#0-2) 

At exactly 1000, `min_fee = 1000 * tx_size / 1000 = tx_size`, which is never zero for any positive `tx_size`. However, `min_fee_rate` is a **per-node operator-configurable field** with no enforced lower bound: [4](#0-3) 

The integration-test template ships with `min_fee_rate = 0`, and the relay test explicitly exercises `min_fee_rate = 0` on one node and `1000` on another: [5](#0-4) [6](#0-5) 

Any operator who sets `min_fee_rate` to a value between 1 and 999 (inclusive) will silently accept zero-fee transactions for all transactions whose serialized size satisfies `min_fee_rate * tx_size < 1000`. For example:

| `min_fee_rate` | `tx_size` threshold where `min_fee = 0` |
|---|---|
| 1 | < 1000 bytes (covers virtually all transactions) |
| 9 | < 112 bytes |
| 99 | < 11 bytes |
| 999 | < 2 bytes |

A typical minimal CKB transaction (one input, one output, no script) serializes to roughly 100–250 bytes, well within the dangerous range for `min_fee_rate` values of 1–9.

The same truncation also affects the RBF extra-fee gate:

```rust
// tx-pool/src/pool.rs  line 103
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
``` [7](#0-6) 

When `min_rbf_rate * size < 1000`, the extra RBF surcharge is also zero, allowing a replacement transaction to pay no incremental fee.

---

### Impact Explanation

A transaction sender (unprivileged tx-pool submitter) can craft a transaction whose serialized size is small enough that `min_fee_rate * tx_size < 1000`. The node then computes `min_fee = 0` and accepts the transaction even if its fee is zero. This:

- Allows zero-fee mempool flooding on any node whose operator has set `min_fee_rate` to a sub-1000 value.
- Silently violates the operator's stated fee policy: the node *believes* it is enforcing a non-zero minimum, but the truncation makes the check a no-op.
- Also nullifies the RBF incremental-fee requirement for small transactions, allowing free replacement cycling.

---

### Likelihood Explanation

The default `min_fee_rate = 1000` is immune (no truncation to zero for any positive `tx_size`). However:

- The configuration field is fully documented and operator-settable with no lower-bound validation.
- The integration-test template ships `min_fee_rate = 0`, normalizing low values.
- Operators running private or low-traffic networks commonly lower fee thresholds.
- The attacker needs only to submit a transaction via the standard `send_transaction` RPC — no special privilege required.

---

### Recommendation

Replace floor division with ceiling division in `FeeRate::fee()`:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    // Ceiling division: (a + b - 1) / b
    let fee = (self.0.saturating_mul(weight).saturating_add(KW - 1)) / KW;
    Capacity::shannons(fee)
}
```

This ensures that any non-zero `min_fee_rate` always produces a `min_fee` of at least 1 shannon for any positive `weight`, eliminating the zero-truncation path. The same fix should be applied wherever `FeeRate::fee()` is used to compute a minimum threshold (including the RBF gate in `calculate_min_replace_fee`).

---

### Proof of Concept

1. Start a CKB node with `min_fee_rate = 5` (5 shannons/KW — a plausible "low-fee" setting).
2. Construct a minimal transaction: one input spending an existing live cell, one output returning the full capacity (fee = 0 shannons). Serialized size ≈ 200 bytes.
3. Compute what the node will check:
   ```
   min_fee = FeeRate(5).fee(200) = (5 * 200) / 1000 = 1000 / 1000 = 1   ← still 1
   ```
   Reduce to `tx_size = 100`:
   ```
   min_fee = (5 * 100) / 1000 = 500 / 1000 = 0   ← zero!
   ```
4. Submit the 100-byte zero-fee transaction via `send_transaction` RPC.
5. Observe: the node accepts it (`fee = 0 >= min_fee = 0`), despite `min_fee_rate = 5 > 0`.
6. Repeat with thousands of distinct zero-fee transactions to flood the mempool at no cost.

The root cause is the floor division at: [1](#0-0) 
consumed by the admission gate at: [2](#0-1)

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

**File:** util/app-config/src/legacy/tx_pool.rs (L9-10)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
```

**File:** util/app-config/src/configs/tx_pool.rs (L14-16)
```rust
    /// txs with lower fee rate than this will not be relayed or be mined
    #[serde(with = "FeeRateDef")]
    pub min_fee_rate: FeeRate,
```

**File:** test/template/ckb.toml (L86-88)
```text
max_tx_pool_size = 180_000_000 # 180mb
min_fee_rate = 0 # Here fee_rate are calculated directly using size in units of shannons/KB
min_rbf_rate = 0 # Here rbf_rate are calculated directly using size in units of shannons/KB
```

**File:** test/src/specs/relay/transaction_relay_low_fee_rate.rs (L49-57)
```rust
        node0.modify_app_config(|config: &mut ckb_app_config::CKBAppConfig| {
            config.tx_pool.min_fee_rate = FeeRate::from_u64(0);
        });
        node0.start();

        node1.modify_app_config(|config: &mut ckb_app_config::CKBAppConfig| {
            config.tx_pool.min_fee_rate = FeeRate::from_u64(1_000);
        });
        node1.start();
```

**File:** tx-pool/src/pool.rs (L102-104)
```rust
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
        // don't account for duplicate txs
```
