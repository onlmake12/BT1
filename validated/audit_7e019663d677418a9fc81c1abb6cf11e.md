Audit Report

## Title
Minimum Fee Rate Check Uses Only Serialized Size, Ignoring Cycles — Allows Sub-Minimum-Fee Transactions Into the Tx-Pool - (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` gates pool admission using only `min_fee_rate × tx_size`, while the effective fee rate stored in every `TxEntry` — and used for block assembly, eviction, and fee estimation — is computed with `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. An attacker can craft a transaction with a small serialized size but near-maximum script execution cycles, pay only the size-based minimum fee, and be admitted to the pool with an effective fee rate orders of magnitude below the configured minimum, forcing expensive VM execution at negligible cost.

## Finding Description

**Root cause — `check_tx_fee` in `tx-pool/src/util.rs` (lines 42–52):**

The function explicitly acknowledges the discrepancy in a comment but treats it as acceptable:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

The gate computes `min_fee = min_fee_rate × tx_size`, ignoring cycles entirely.

**Actual weight used everywhere else — `get_transaction_weight` in `util/types/src/core/tx_pool.rs` (lines 298–303):**

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**Effective fee rate stored in `TxEntry` — `tx-pool/src/component/entry.rs` (lines 114–118):**

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

**Processing pipeline — `tx-pool/src/process.rs`:**

`pre_check` (line 269) calls `check_tx_fee` (size-only gate) before script verification. Actual cycles are only known after `verify_rtx` returns (line 724–732). The `TxEntry` is then constructed at line 751 with the real cycles (`TxEntry::new(rtx, verified.cycles, fee, tx_size)`), but no post-verification check validates the true effective fee rate against `min_fee_rate`. For remote transactions, `declared_cycles` is available at `_process_tx` (line 708) but is never passed to `pre_check` or `check_tx_fee`.

**Concrete numbers:**

| Parameter | Value |
|---|---|
| `min_fee_rate` (default) | 1,000 shannons/KB |
| `DEFAULT_BYTES_PER_CYCLES` | 0.000_170_571_4 |
| `max_tx_verify_cycles` | 70,000,000 |

Craft a transaction with `tx_size = 200 bytes`, `cycles = 70,000,000`:
- **Gate check**: `min_fee = 1,000 × 200 / 1,000 = 200 shannons` → pay exactly 200 shannons → **passes**
- **Actual weight**: `max(200, 70,000,000 × 0.000_170_571_4) ≈ 11,940`
- **Effective fee rate**: `200 × 1,000 / 11,940 ≈ 16 shannons/KB` — **59× below the minimum**

## Impact Explanation

**High — "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

1. **CPU resource exhaustion**: Each admitted transaction forces the node to run the full CKB-VM script verifier for up to `max_block_cycles` cycles. An attacker paying only 200 shannons per transaction triggers 70,000,000 cycles of VM execution — a ~59× amplification in computational cost relative to what the fee rate minimum was designed to enforce. Sustained submission degrades node performance and can cause network congestion.

2. **Tx-pool flooding**: The pool fills with entries whose effective fee rates are far below `min_fee_rate`, displacing legitimate transactions when `max_tx_pool_size` is reached.

3. **Fee estimation distortion**: `estimate_fee_rate` in `pool_map.rs` (lines 334–358) iterates pool entries by their stored `fee_rate()` (which uses real weight). A pool flooded with artificially low-rate entries skews fee estimates downward, misleading honest users.

## Likelihood Explanation

The attack requires no special privilege. Any node reachable via the JSON-RPC `send_transaction` endpoint or the P2P relay protocol (`RelayTransactions`) can submit such transactions. For P2P relay, `declared_cycles` must match actual cycles (enforced by `DeclaredWrongCycles` at line 736–748 of `process.rs`), but the attacker simply declares the true high cycle count — the fee check still uses only size. The only cost is the size-based minimum fee (e.g., 200 shannons per transaction). The attacker can sustain a continuous stream of high-cycle, small-size transactions at negligible cost.

## Recommendation

Replace the size-only minimum fee gate in `check_tx_fee` with a weight-aware check. For remote transactions where `declared_cycles` is available before verification, pass it through `pre_check` and use it immediately:

```rust
let weight = if let Some(cycles) = declared_cycles {
    get_transaction_weight(tx_size, cycles)
} else {
    tx_size as u64
};
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

For local transactions (no declared cycles), add a post-verification check in `_process_tx` after `verified.cycles` is known, rejecting the entry if its true effective fee rate falls below `min_fee_rate`.

## Proof of Concept

1. Construct a CKB transaction whose lock script runs a tight computation loop consuming ~70,000,000 cycles but whose serialized size is ~200 bytes.
2. Set outputs capacity so that `inputs_capacity − outputs_capacity = 200 shannons` (exactly `min_fee_rate × tx_size`).
3. Submit via `send_transaction` RPC.
4. Observe the transaction is accepted into the pending pool.
5. Query `get_transaction` and inspect the entry: the stored cycles will be ~70,000,000, giving an effective fee rate of ~16 shannons/KB — well below the 1,000 shannons/KB minimum.
6. Repeat in a loop; each iteration costs ~200 shannons but forces ~70,000,000 cycles of VM execution on the node, causing sustained CPU load and eventual pool congestion.