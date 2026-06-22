### Title
Integer Division Truncation in `FeeRate::fee()` Silently Zeroes Minimum Fee, Bypassing Tx-Pool Admission Check — (File: `util/types/src/core/fee_rate.rs`)

---

### Summary

When a node operator configures a low `min_fee_rate`, integer division truncation in `FeeRate::fee()` causes the computed minimum fee threshold to silently become zero. The tx-pool admission check then passes for any transaction — including zero-fee ones — allowing an unprivileged tx-pool submitter to flood the pool with feeless transactions.

---

### Finding Description

In `util/types/src/core/fee_rate.rs`, the `fee()` method computes the minimum fee required for a given transaction weight:

```rust
// util/types/src/core/fee_rate.rs, line 34-37
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;   // KW = 1000
    Capacity::shannons(fee)
}
```

When `self.0 * weight < 1000`, integer division truncates the result to `0`, producing a `min_fee` of zero shannons.

This computed value is used directly as the enforcement threshold in `tx-pool/src/util.rs`:

```rust
// tx-pool/src/util.rs, lines 45-52
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
// reject txs which fee lower than min fee rate
if fee < min_fee {
    let reject =
        Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
    ...
    return Err(reject);
}
```

If `min_fee_rate` is set to a low value (e.g., `1` shannon/KW) and the transaction's serialized size is less than 1000 bytes, `min_fee` computes to `0`. The guard `fee < 0` is always false for any non-negative `Capacity`, so **zero-fee transactions are unconditionally admitted** to the tx-pool, regardless of the operator's intent.

A typical minimal CKB transaction (one input, one output, secp256k1 lock) serializes to roughly 200–500 bytes — well below the 1000-byte threshold at which the truncation disappears.

---

### Impact Explanation

An unprivileged attacker who knows (or probes) that a target node has a low `min_fee_rate` can submit an unbounded stream of zero-fee transactions via the `send_transaction` RPC. Each transaction passes `check_tx_fee` because `min_fee` rounds to zero. The tx-pool grows without bound until it hits its capacity limit, evicting legitimate fee-paying transactions and degrading or halting block assembly for that node.

---

### Likelihood Explanation

Medium. The operator must have set `min_fee_rate` to a value low enough that `fee_rate * tx_size < 1000`. This is a plausible operator mistake — analogous to the external report's authority setting a repurchase price too low — because the configuration accepts any `u64` value with no enforced minimum, and the truncation is silent: the node logs no warning and the fee-rate field in the rejection error still shows the non-zero configured rate, masking the actual effective threshold of zero.

---

### Recommendation

Apply ceiling division in `FeeRate::fee()` so that any non-zero fee rate always produces a minimum fee of at least 1 shannon:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    // ceiling: (rate * weight + KW - 1) / KW
    let fee = self.0.saturating_mul(weight).saturating_add(KW - 1) / KW;
    Capacity::shannons(fee)
}
```

Alternatively, enforce a minimum `min_fee_rate` value (e.g., `≥ 1000` shannons/KW) during config validation so that the computed `min_fee` is always at least 1 shannon for any realistic transaction size.

---

### Proof of Concept

1. Configure a CKB node with `min_fee_rate = 1` (1 shannon/KW) in `ckb.toml`.
2. Craft a transaction where `inputs_capacity == outputs_capacity` (fee = 0 shannons), serialized size ~300 bytes.
3. Submit via `send_transaction` RPC.
4. Inside `check_tx_fee` (`tx-pool/src/util.rs:45`): `min_fee = FeeRate(1).fee(300) = (1 * 300) / 1000 = 0`.
5. The guard `0 < 0` is false → transaction is admitted.
6. Repeat in a tight loop; the tx-pool fills with zero-fee transactions, starving legitimate traffic.

---

**Root cause location:** [1](#0-0) 

**Enforcement site where the zeroed threshold is consumed:** [2](#0-1)

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
