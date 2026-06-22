### Title
`FeeRate::fee()` Integer Division Rounds Minimum Fee to Zero, Allowing Zero-Fee Transactions to Bypass `min_fee_rate` Admission Check — (`tx-pool/src/util.rs`, `util/types/src/core/fee_rate.rs`)

---

### Summary

`FeeRate::fee()` computes the minimum required fee using integer division `(min_fee_rate * weight) / 1000`. When `min_fee_rate * tx_size < 1000`, this rounds down to zero. The admission gate in `check_tx_fee()` then evaluates `fee < 0` (a u64 comparison that is always false), so any transaction — including a zero-fee one — passes the minimum-fee-rate check. An unprivileged RPC caller can exploit this to inject zero-fee transactions into the mempool of any node configured with a small but non-zero `min_fee_rate`, bypassing the spam-prevention floor entirely.

---

### Finding Description

**Root cause — `FeeRate::fee()` floor division:** [1](#0-0) 

```rust
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;   // KW = 1000
    Capacity::shannons(fee)
}
```

When `self.0 * weight < 1000`, the result is `0`. For example, with `min_fee_rate = 1` (1 shannon/KW) and a 242-byte transaction: `1 × 242 / 1000 = 0`.

**Admission gate — `check_tx_fee()`:** [2](#0-1) 

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {          // fee: Capacity (u64), min_fee: Capacity(0)
    return Err(reject);     // 0 < 0 is false → check silently passes
}
```

Because `Capacity` is a `u64` wrapper, `fee < Capacity::shannons(0)` is `fee < 0`, which is always `false`. A zero-fee transaction (inputs_capacity == outputs_capacity) therefore passes unconditionally.

**Call path:**

`send_transaction` RPC → `pre_check()` → `check_tx_fee()` → `FeeRate::fee()` → returns 0 → gate passes. [3](#0-2) 

**Condition for rounding to zero:**

`min_fee_rate * tx_size < 1000`

| `min_fee_rate` | Max tx size that rounds to 0 |
|---|---|
| 1 | 999 bytes (covers nearly all transactions) |
| 9 | 111 bytes |
| 100 | 9 bytes (unreachable in practice) |

The default is 1000 shannons/KW, which never rounds to zero for real transactions. However, any operator who sets `min_fee_rate` to a small non-zero value (e.g., 1–9) to allow "almost free" transactions will unknowingly accept fully zero-fee transactions. [1](#0-0) 

---

### Impact Explanation

An attacker who discovers (by probing) that a node accepts zero-fee transactions can:

1. **Flood the mempool** with zero-fee transactions at negligible cost, consuming memory and CPU verification resources.
2. **Displace legitimate transactions** from the bounded mempool (`max_tx_pool_size = 180 MB` default), degrading service for honest users.
3. **Propagate spam to peers** — the relay protocol forwards accepted transactions to connected nodes, amplifying the effect across the network segment.

The `min_fee_rate` guard is the primary spam-prevention mechanism at the tx-pool admission layer. Silently nullifying it undermines the node's DoS resistance. [4](#0-3) 

---

### Likelihood Explanation

The vulnerability requires `min_fee_rate` to be set below `1000 / tx_size`. The default is 1000 shannons/KW, which is safe. However:

- Operators who want to allow "low-fee" transactions may set values like 1–100.
- The configuration field is documented only as "shannons per KB"; an operator setting `min_fee_rate = 1` reasonably expects at least 1 shannon to be required, not zero.
- The attacker can probe any public node by submitting a zero-fee transaction and observing acceptance.
- No privileged access, key material, or majority hashpower is required. [5](#0-4) 

---

### Recommendation

Replace floor division with ceiling division in `FeeRate::fee()` so that any non-zero `min_fee_rate` always produces a minimum fee of at least 1 shannon:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    // Ceiling division: (a + b - 1) / b
    let fee = self.0.saturating_mul(weight).saturating_add(KW - 1) / KW;
    Capacity::shannons(fee)
}
```

Alternatively, add an explicit guard in `check_tx_fee()`:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
// If min_fee_rate is non-zero but rounds to zero, enforce at least 1 shannon
let min_fee = if min_fee == Capacity::zero() && !tx_pool.config.min_fee_rate.is_zero() {
    Capacity::one()
} else {
    min_fee
};
``` [1](#0-0) 

---

### Proof of Concept

**Setup:** Node configured with `min_fee_rate = 1` (1 shannon/KW).

**Attack:**
1. Obtain a live cell with capacity C shannons.
2. Construct a transaction spending that cell and returning exactly C shannons to an output (zero fee: `inputs_capacity == outputs_capacity`).
3. Submit via `send_transaction` RPC.

**Expected (correct) behavior:** Rejected with `LowFeeRate`.

**Actual behavior:** Accepted. Trace:
- `tx_size ≈ 242` bytes
- `min_fee = FeeRate(1).fee(242) = 1 * 242 / 1000 = 0`
- `fee = 0 < min_fee = 0` → `false` → transaction admitted

Repeat with thousands of distinct inputs to exhaust mempool capacity. [6](#0-5) [1](#0-0)

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

**File:** util/app-config/src/legacy/tx_pool.rs (L9-10)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
```
