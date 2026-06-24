Audit Report

## Title
Incomplete Fee-Rate Admission Check Omits Cycles Component, Allowing Below-Minimum-Fee-Rate Transactions Into the Pool — (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` validates the minimum fee using only the transaction's serialized byte size, while the canonical weight used everywhere else is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A submitter can craft a small-serialized-size, high-cycles transaction whose fee satisfies `fee ≥ min_fee_rate × size` yet whose true fee rate (computed with proper weight) is far below `min_fee_rate`. No second fee-rate gate is applied after cycles become known, so the transaction is admitted and persists in the pool.

## Finding Description

`check_tx_fee` computes the minimum required fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code comment itself acknowledges the discrepancy. The canonical weight function used everywhere else is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

`TxEntry::fee_rate()` uses this proper weight for sorting, eviction, and fee estimation. The full submission flow in `_process_tx` is:

1. `pre_check` → calls `check_tx_fee` with size only (`process.rs` lines 289/294)
2. `verify_rtx` → executes scripts, returns actual `verified.cycles` (line 724)
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry constructed with real cycles (line 751)
4. `submit_entry` — no second fee-rate check applied (line 753)

There is no gate between steps 2 and 4 that re-evaluates the fee rate using the now-known cycles value. `submit_entry` only performs RBF checks and context-change re-verification; it contains no fee-rate validation.

## Impact Explanation

An attacker submitting a 200-byte transaction consuming 5,000,000 cycles pays only `min_fee_rate × 200 / 1000 = 200 shannons` to pass `check_tx_fee`. The true weight is `max(200, 5_000_000 × 0.000_170_571_4) ≈ 852`, giving a true fee rate of `≈ 236 shannons/KW` against a `min_fee_rate` of `1000 shannons/KW`. The transaction enters the pool at ~4× below the minimum threshold.

The primary impact is **CPU DoS / network congestion with few costs**: each submission triggers a full `verify_rtx` execution (up to `max_tx_verify_cycles` cycles of script execution) for a transaction that the attacker paid far less than proper weight-based admission would require. The attacker can sustain this indefinitely, continuously resubmitting at negligible cost (`min_fee_rate × size` shannons per attempt), forcing the node to expend disproportionate CPU resources. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"** (High, 10001–15000 points).

Secondary effects include pool pollution (below-threshold-fee-rate transactions occupy pool slots, displacing legitimate transactions) and fee estimation distortion (`estimate_fee_rate` and `FeeRateCollector` use proper weight, so admitted low-fee-rate transactions distort the fee market signal).

## Likelihood Explanation

Any node with a public RPC endpoint is reachable via `send_transaction` with no privilege required. Crafting a small-serialized-size, high-cycles transaction is straightforward for any script author. The attack is cheap and repeatable: the attacker pays only `min_fee_rate × size` shannons per submission, which is a fraction of the fee that proper weight-based admission would require. The attack can be sustained indefinitely.

## Recommendation

After `verify_rtx` returns the actual cycles in `_process_tx`, apply a second fee-rate check using the proper weight before constructing the `TxEntry`:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(tx_pool_config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64())), snapshot));
}
```

This mirrors the pattern already used in `TxEntry::fee_rate()` and `estimate_fee_rate` in `pool_map.rs`. The existing size-only check in `check_tx_fee` can remain as the cheap pre-verification gate; the weight-based check becomes the authoritative post-verification gate.

## Proof of Concept

1. Construct a CKB transaction with a lock script that runs a tight loop consuming ~5,000,000 cycles. Serialized size: ~200 bytes.
2. Set outputs capacity such that `fee = 201 shannons` (just above `min_fee_rate × 200 / 1000 = 200 shannons` at `min_fee_rate = 1000 shannons/KW`).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200 shannons`; `201 ≥ 200` → passes.
5. `verify_rtx` executes the script, returning `cycles = 5_000_000`.
6. `TxEntry` is created; `fee_rate() = FeeRate::calculate(201, max(200, 852)) ≈ 236 shannons/KW` — well below `min_fee_rate = 1000 shannons/KW`.
7. The transaction is now in the pool with a fee rate ~4× below the minimum threshold.
8. Repeat continuously: each iteration costs ~201 shannons and forces the node to execute 5,000,000 cycles of script verification.