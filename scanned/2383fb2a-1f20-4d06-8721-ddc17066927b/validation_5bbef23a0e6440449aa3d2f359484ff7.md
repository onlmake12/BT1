Audit Report

## Title
`check_tx_fee` Uses `tx_size`-Only Weight for Admission Gate, Allowing Cycle-Heavy Transactions to Bypass Minimum Fee Rate — (`tx-pool/src/util.rs`)

## Summary

`check_tx_fee` enforces the minimum fee rate using only serialized transaction size, while the pool's actual weight metric is `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. An attacker can craft a cycle-heavy, byte-light transaction that passes the admission gate at a fee ~119× below the intended minimum, forcing full script verification (up to 70M cycles) per transaction at drastically reduced cost. The same flaw exists in `calculate_min_replace_fee` for RBF.

## Finding Description

In `tx-pool/src/util.rs` at L42–45, `check_tx_fee` computes the minimum required fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The developer comment acknowledges the discrepancy but treats it as an acceptable approximation. However, the actual weight used for pool ordering, eviction, and fee-rate statistics is defined in `util/types/src/core/tx_pool.rs` at L298–303:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

`TxEntry::fee_rate()` in `tx-pool/src/component/entry.rs` at L115–118 uses this correct weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

The exploit path through `_process_tx` in `tx-pool/src/process.rs` is:
1. L715: `pre_check` → calls `check_tx_fee` with size-only weight → **passes** for cycle-heavy tx
2. L724–732: `verify_rtx` → full script execution up to 70M cycles → **consumes full CPU**
3. L751: `TxEntry::new(rtx, verified.cycles, fee, tx_size)` → entry admitted with correct cycles recorded
4. Pool ordering/eviction now sees the true low fee rate via `fee_rate()` / `EvictKey`

The same size-only assumption in `calculate_min_replace_fee` at `tx-pool/src/pool.rs` L103:
```rust
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```
allows a cycle-heavy replacement transaction to satisfy the RBF extra-fee requirement based on size alone.

## Impact Explanation

With `max_tx_verify_cycles = 70,000,000` and `DEFAULT_BYTES_PER_CYCLES ≈ 0.000_170_571_4`:
- A transaction with `tx_size = 100` bytes and `cycles = 70,000,000` has `actual_weight = max(100, 11,940) = 11,940`
- Admission check requires: `1000 × 100 / 1000 = 100 shannons`
- Correct weight-based minimum: `1000 × 11,940 / 1000 = 11,940 shannons`
- **Cost reduction: ~119×**

An attacker can flood the mempool with cycle-heavy, byte-light transactions at ~1/119th the intended minimum cost. Each such transaction forces nodes to spend 70M cycles on script verification before admission. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10001–15000 points)**. The verification pipeline is a shared resource; exhausting it delays processing of all legitimate transactions across the network.

## Likelihood Explanation

Any unprivileged user with access to the `send_transaction` JSON-RPC or P2P relay can trigger this. No special privileges, keys, or majority hashpower are required. The attacker needs only UTXOs (to construct valid transactions) and a script that loops for ~70M cycles but encodes to ~100 bytes — a straightforward construction. The attack is repeatable as long as the attacker holds UTXOs, and each iteration forces 70M cycles of CPU on every receiving node.

## Recommendation

Replace the size-only weight in `check_tx_fee` with the actual transaction weight. Since cycles are not yet known at the pre-verification admission stage, two options exist:

1. **Use declared cycles pre-verification**: Pass `declared_cycles` (already available in `_process_tx` at L708 and passed through `pre_check`) into `check_tx_fee` and compute `min_fee = min_fee_rate.fee(get_transaction_weight(tx_size, declared_cycles))`. For local RPC submissions where `declared_cycles` is `None`, use `max_block_cycles` as a conservative upper bound.
2. **Re-check post-verification**: After `verify_rtx` returns `verified.cycles`, perform a second fee check using `get_transaction_weight(tx_size, verified.cycles)` before calling `submit_entry`.

Apply the same fix to `calculate_min_replace_fee` in `tx-pool/src/pool.rs`, passing the replacement transaction's declared or verified cycles alongside its size.

## Proof of Concept

1. Obtain a UTXO on a CKB node with default `min_fee_rate = 1000 shannons/KW` and `max_tx_verify_cycles = 70,000,000`.
2. Write a lock script that executes a tight loop consuming exactly 69,999,999 cycles; compile it to a minimal binary (~50–80 bytes).
3. Construct a transaction spending the UTXO with this lock script. Serialized `tx_size ≈ 100` bytes.
4. Set `fee = 101 shannons` (just above `1000 × 100 / 1000 = 100`).
5. Submit via `send_transaction` RPC (no declared cycles required).
6. Observe: `check_tx_fee` computes `min_fee = 100 shannons`; transaction passes admission.
7. Node executes ~70M cycles of script verification.
8. `TxEntry::fee_rate()` reports `FeeRate::calculate(101, 11940) ≈ 8 shannons/KW` — far below the 1000 shannons/KW minimum.
9. Repeat with many UTXOs to saturate the verification pipeline and fill the pool with ~8 shannons/KW entries at ~101 shannons each, instead of the intended ~11,940 shannons each.
10. Verify that legitimate transactions experience increased latency or rejection due to pool saturation and verification queue exhaustion.