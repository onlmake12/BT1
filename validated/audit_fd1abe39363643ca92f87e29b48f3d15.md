Audit Report

## Title
Fee-Rate Unit Mismatch in `calculate_min_replace_fee` and `check_tx_fee` Allows RBF Policy Bypass and Pool Admission Bypass — (`tx-pool/src/pool.rs`, `tx-pool/src/util.rs`)

## Summary

`FeeRate` is defined as shannons per kilo-weight, and `FeeRate::fee(weight)` expects `weight = max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. Both `calculate_min_replace_fee` and `check_tx_fee` pass raw `tx_size` bytes instead of the correct weight. For a high-cycles, small-size transaction this underestimates the required fee by up to ~60×, allowing an attacker to bypass RBF policy and pool admission fee-rate enforcement with minimal cost.

## Finding Description

**`FeeRate` unit and `fee()` semantics:** `FeeRate` is shannons per kilo-weight; `fee(weight)` computes `rate × weight / 1000`. [1](#0-0) [2](#0-1) 

**Correct weight definition:** `get_transaction_weight` returns `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. [3](#0-2) 

**Root cause 1 — `calculate_min_replace_fee`:** The function signature accepts only `size: usize` and passes it directly to `min_rbf_rate.fee()`, ignoring cycles entirely. [4](#0-3) 

At the call site in `check_rbf`, the `entry` is a fully-constructed `TxEntry` with both `entry.size` and `entry.cycles` available, but only `entry.size` is forwarded. [5](#0-4) 

**Root cause 2 — `check_tx_fee`:** The code explicitly acknowledges the mismatch in a comment ("cheap check") and uses `tx_size as u64` as the weight argument to `min_fee_rate.fee()`. [6](#0-5) 

**No corrective check after cycles are known:** After `verify_rtx` returns actual cycles, a `TxEntry` is constructed and `submit_entry` is called — but no weight-based fee-rate check is performed at this point. [7](#0-6) 

**`TxEntry::fee_rate()` uses correct weight** but is only used for sorting/eviction, not for admission or RBF gating. [8](#0-7) 

## Impact Explanation

**RBF bypass (primary):** An attacker crafts a replacing transaction with small serialized size (~200 bytes) but a lock script consuming near-maximum cycles (70,000,000). `calculate_min_replace_fee` computes the required fee bump using size-only weight (200), yielding `extra_rbf_fee = 1500 × 200 / 1000 = 300 shannons`. The correct weight is `max(200, 70,000,000 × 0.000170571) = 11,940`, so the correct extra fee should be `1500 × 11,940 / 1000 = 17,910 shannons` — a 60× shortfall. The replacing transaction is accepted, evicting the victim, but has an effective fee rate of ~41 shannons/KW, far below `min_rbf_rate`, so miners will not include it. This enables indefinite, low-cost griefing of specific transactions.

**Pool admission bypass (secondary):** Any `send_transaction` caller can submit high-cycles, small-size transactions with fee just above `min_fee_rate.fee(size)`. Each is admitted with an effective fee rate potentially 60× below `min_fee_rate`, wastes a pool slot, and consumes significant CPU during script verification.

This matches: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** [9](#0-8) 

## Likelihood Explanation

- RBF is enabled by default (`min_rbf_rate = 1_500 > min_fee_rate = 1_000`).
- The attack requires only an unprivileged `send_transaction` RPC call.
- Crafting a RISC-V loop consuming near-maximum cycles is straightforward in CKB-VM.
- `max_tx_verify_cycles = 70_000_000` is the only upper bound, producing a ~60× weight discrepancy for a minimal-size transaction.
- The attack is repeatable: after each replacement, the attacker re-submits to keep the victim's transaction out of the pool indefinitely. [9](#0-8) 

## Recommendation

**For `calculate_min_replace_fee`:** Add a `cycles: u64` parameter, compute the correct weight via `get_transaction_weight`, and pass it to `fee()`:

```rust
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize, cycles: u64) -> Option<Capacity> {
    let weight = get_transaction_weight(size, cycles);
    let extra_rbf_fee = self.config.min_rbf_rate.fee(weight);
    ...
}
```

Update the call site at `pool.rs:665` to pass `entry.cycles` alongside `entry.size`. [10](#0-9) 

**For `check_tx_fee`:** Move the fee-rate check to after `verify_rtx` returns actual cycles, using `get_transaction_weight(tx_size, verified.cycles)` as the weight argument. The current size-only check is the sole admission gate and must not be left as the final word. [6](#0-5) 

## Proof of Concept

1. **Setup:** Node with default config (`min_fee_rate = 1000`, `min_rbf_rate = 1500`, `max_tx_verify_cycles = 70_000_000`).

2. **Submit victim tx1:** Serialized size ≈ 200 bytes, cycles ≈ 1,000, fee = 200 shannons. Effective fee rate = `200 × 1000 / 200 = 1000 shannons/KW` — passes.

3. **Craft attacker tx2:** Same inputs as tx1 (conflict), lock script is a RISC-V loop consuming 70,000,000 cycles, serialized size ≈ 200 bytes.
   - `check_tx_fee`: `min_fee = 1000 × 200 / 1000 = 200 shannons`. Pay 501 shannons → passes.
   - `calculate_min_replace_fee`: `extra_rbf_fee = 1500 × 200 / 1000 = 300 shannons`. `min_replace_fee = 200 + 300 = 500 shannons`. Pay 501 shannons → passes RBF check.

4. **Actual weight of tx2:** `max(200, 70,000,000 × 0.000170571) = 11,940`.
   - Effective fee rate = `501 × 1000 / 11,940 ≈ 41 shannons/KW` — 24× below `min_fee_rate`, 36× below `min_rbf_rate`.

5. **Result:** tx1 is evicted. tx2 occupies the pool with ~41 shannons/KW effective fee rate. Miners sort by fee rate and will not include tx2, indefinitely delaying the original transaction. The correct extra RBF fee should have been 17,910 shannons; the attacker paid 300 — a 60× shortfall. [4](#0-3) [11](#0-10)

### Citations

**File:** util/types/src/core/fee_rate.rs (L3-7)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;
```

**File:** util/types/src/core/fee_rate.rs (L33-37)
```rust
    /// Calculates the fee for a given weight.
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/pool.rs (L101-103)
```rust
    /// min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```

**File:** tx-pool/src/pool.rs (L662-676)
```rust
        // Rule #4, new tx's fee need to higher than min_rbf_fee computed from the tx_pool configuration
        // Rule #3, new tx's fee need to higher than conflicts, here we only check the all conflicted txs fee
        let fee = entry.fee;
        if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
            if fee < min_replace_fee {
                return Err(Reject::RBFRejected(format!(
                    "Tx's current fee is {}, expect it to >= {} to replace old txs",
                    fee, min_replace_fee,
                )));
            }
        } else {
            return Err(Reject::RBFRejected(
                "calculate_min_replace_fee failed".to_string(),
            ));
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

**File:** tx-pool/src/process.rs (L751-753)
```rust
        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** resource/ckb.toml (L211-215)
```text
max_tx_pool_size = 180_000_000 # 180mb
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
max_tx_verify_cycles = 70_000_000
```
