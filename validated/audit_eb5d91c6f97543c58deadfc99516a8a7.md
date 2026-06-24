Audit Report

## Title
Minimum Fee Rate Admission Check Uses `tx_size` Instead of Weight, Allowing Cycle-Heavy Transactions to Bypass Fee Rate Enforcement - (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using only the raw serialized byte size (`tx_size`) of a transaction, rather than the correct weight defined as `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. The code itself acknowledges this is a deliberate approximation ("cheap check"), but the consequence is that a transaction with small byte size and very high cycles can pass the fee gate while its true fee rate is orders of magnitude below `min_fee_rate`. The pool then stores and processes such transactions, consuming block cycle budget at negligible cost to the attacker.

## Finding Description
`FeeRate` is defined as shannons per kilo-weight, and the canonical weight formula is:

```rust
// util/types/src/core/tx_pool.rs L298-303
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

This formula is used correctly in `TxEntry::fee_rate()`, `AncestorsScoreSortKey`, `EvictKey`, and the fee-rate statistics RPC. However, the admission gate `check_tx_fee` deviates:

```rust
// tx-pool/src/util.rs L42-45
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

`check_tx_fee` is called from `pre_check` (lines 289, 294 of `process.rs`) before script verification, so cycles are not yet known. The function signature only accepts `tx_size: usize` — there is no cycles parameter. After verification completes in `_process_tx`, the actual cycles are known (line 751: `TxEntry::new(rtx, verified.cycles, fee, tx_size)`), but no second fee-rate check against weight is performed at that point.

The default `max_tx_verify_cycles` is `TWO_IN_TWO_OUT_CYCLES * 20 = 70,000,000` cycles (from `tx_pool.rs` legacy config). For a local RPC submission (`send_transaction`), `declared_cycles` is `None`, so `max_cycles = self.consensus.max_block_cycles()` = `3,500,000,000` cycles. A transaction can therefore have up to `3.5B` cycles verified by the pool.

**Exploit path:**
1. Craft a transaction: serialized size ≈ 200 bytes, actual cycles ≈ 3,500,000,000 (near `MAX_BLOCK_CYCLES`).
2. Set fee = 201 shannons.
3. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200 shannons`. Fee 201 > 200 → **admitted**.
4. Actual weight = `max(200, 3_500_000_000 × 0.000_170_571_4)` ≈ 596,999.
5. True fee rate = `201 × 1000 / 596_999` ≈ **0.34 shannons/KW** — ~3000× below `min_fee_rate`.
6. The transaction occupies the pool and saturates the block cycle budget.

The `DeclaredWrongCycles` check (process.rs L736-748) only applies to relayed transactions where `declared_cycles` is provided by the peer. For local RPC (`send_transaction`), `declared_cycles` is `None`, so this check is skipped entirely.

## Impact Explanation
This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**.

An attacker can flood the tx-pool with transactions that each consume nearly the entire block cycle budget (`MAX_BLOCK_CYCLES = 3,500,000,000`) while paying only ~200 shannons per transaction. Each such transaction, once proposed and committed, prevents all other transactions from being included in that block. With the pool size at 180 MB and each transaction being ~200 bytes, an attacker could submit ~900,000 such transactions. Even a small number (a few dozen) would saturate the cycle budget for many consecutive blocks, causing severe network congestion and degrading throughput for all users. The cost to the attacker is negligible.

## Likelihood Explanation
The attack is reachable via the standard `send_transaction` RPC, which is accessible to any local or trusted RPC caller (default config binds to `127.0.0.1:8114`). No special privilege, key, or majority hashpower is required beyond RPC access. The attacker only needs to know `min_fee_rate` (visible via `tx_pool_info` RPC) and craft a transaction with a computationally heavy script (e.g., a loop-heavy CKB-VM script). The attack is repeatable and cheap. The code comment explicitly acknowledges the approximation, confirming this is a known design gap rather than an implementation accident.

## Recommendation
After script verification completes in `_process_tx` (where `verified.cycles` is available), perform a second, accurate fee-rate check using the full weight before calling `submit_entry`:

```rust
// After verified.cycles is known, before submit_entry:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

The existing `check_tx_fee` call in `pre_check` can remain as a cheap lower-bound guard (using `tx_size`), but a definitive weight-based check must be added post-verification.

## Proof of Concept
1. Configure node with default `min_fee_rate = 1000` shannons/KW.
2. Write a CKB script that performs a tight computation loop consuming ~3,499,999,000 cycles.
3. Build a transaction using that script: serialized size ≈ 200 bytes, fee = 201 shannons.
4. Submit via `send_transaction` RPC (no declared cycles needed).
5. `check_tx_fee` passes: `min_fee = 1000 × 200 / 1000 = 200`, fee 201 > 200.
6. `verify_rtx` runs with `max_cycles = consensus.max_block_cycles() = 3_500_000_000`, script executes successfully consuming ~3.5B cycles.
7. No declared cycles → `DeclaredWrongCycles` check is skipped.
8. Transaction enters pool with `fee_rate() ≈ 0.34 shannons/KW`.
9. Repeat with chained UTXOs to fill the pool with cycle-saturating, near-zero-fee-rate transactions.
10. Each block can include at most one such transaction (cycle budget exhausted), blocking all legitimate transactions.