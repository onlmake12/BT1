Audit Report

## Title
Min-Fee-Rate Admission Check Uses Only Serialized Size, Ignoring Cycle-Based Weight — Cycle-Heavy Transactions Bypass Effective Fee Floor - (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size, while the correct fee-rate metric throughout the rest of the codebase is `weight = max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. Because cycles are unavailable before script execution and no post-verification fee-rate re-check is performed, a transaction with a small serialized size but near-maximum cycle consumption passes the admission gate with a fee that is a fraction of the true minimum. The code itself documents this gap with an explicit comment acknowledging it is a "cheap check."

## Finding Description

**Admission gate (`tx-pool/src/util.rs`, L42–52):**

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

`check_tx_fee` is called inside `pre_check` (`tx-pool/src/process.rs`, L289 and L294), which runs **before** `verify_rtx` (L724–732). At that point, cycles are not yet known, so only `tx_size` is passed.

After `verify_rtx` returns with `verified.cycles`, the code at `process.rs` L751 creates `TxEntry::new(rtx, verified.cycles, fee, tx_size)` and proceeds to `submit_entry`. There is **no second fee-rate check** using the actual cycles.

**Correct weight formula (`util/types/src/core/tx_pool.rs`, L298–303):**

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (L279). This formula is used by `TxEntry::fee_rate()` (entry.rs L114–118), `AncestorsScoreSortKey` (entry.rs L221–231), and `EvictKey` (entry.rs L234–247) — everywhere **after** admission.

The result is a structural inconsistency: the admission gate uses size-only fee rate, while all post-admission scheduling and eviction use weight-based fee rate. A transaction admitted under the size-only gate can have an effective weight-based fee rate far below `min_fee_rate`.

## Impact Explanation

**Concrete example** (default config: `min_fee_rate = 1000 shannons/KW`, `max_tx_verify_cycles = 70_000_000`):

| Metric | Value |
|---|---|
| `tx_size` | 597 bytes |
| `cycles` | 70,000,000 |
| Size-based `min_fee` | 597 shannons → **admitted** |
| Correct `weight` | `max(597, 70_000_000 × 0.000_170_571_4) = 11,940` |
| Weight-based `min_fee` | 11,940 shannons |
| Effective fee rate | ~50 shannons/KW — **20× below the 1,000 floor** |

Each such transaction forces the node to execute up to 70M cycles of script verification work before the true fee rate is known. The transaction is then relayed to peers, who also execute the full 70M-cycle verification. An attacker can sustain this at 1/20th the intended cost, causing sustained wasted verification resources across the network.

This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

Any unprivileged user can submit transactions via the `send_transaction` RPC. Crafting a transaction with a computationally expensive lock script (consuming ~70M cycles) and a small serialized body is straightforward — it requires only the attacker's own UTXOs and no special privileges. The attack is repeatable as long as the attacker controls UTXOs, and each iteration costs only ~597 shannons instead of ~11,940 shannons. The 20× cost reduction makes sustained pool-pollution and peer-relay amplification economically viable.

## Recommendation

After `verify_rtx` returns and actual cycles are known, perform a second fee-rate check using the correct weight before creating the `TxEntry`:

```rust
// In _process_tx, after verify_rtx returns `verified`:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(
        tx_pool_config.min_fee_rate,
        min_fee_by_weight.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

This mirrors how `TxEntry::fee_rate()` and `AncestorsScoreSortKey` already compute the effective fee rate and closes the gap between the admission gate and the actual scheduling metric.

## Proof of Concept

1. Deploy a lock script that loops to consume ~70,000,000 cycles.
2. Craft a transaction spending a UTXO locked by that script, with serialized size ~597 bytes.
3. Set fee = 597 shannons (exactly `min_fee_rate.fee(597)`).
4. Submit via `send_transaction` RPC.
5. `check_tx_fee` computes `min_fee = 1000 × 597 / 1000 = 597 shannons`. Fee equals min_fee → **admitted**.
6. `verify_rtx` executes 70M cycles of script work on the local node.
7. Transaction is relayed to peers; each peer also executes 70M cycles.
8. Actual weight = `max(597, 70_000_000 × 0.000_170_571_4) = 11,940`. Effective fee rate ≈ 50 shannons/KW — 20× below the configured floor.
9. Transaction sits at the bottom of the priority queue (correctly ranked by weight-based fee rate post-admission) and is eventually evicted, but not before consuming verification resources on the local node and all relay peers.
10. Repeat with fresh UTXOs to sustain the attack at 1/20th the intended cost.