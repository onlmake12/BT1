Audit Report

## Title
Single-Dimension Fee Rate Admission Check Ignores Cycle Weight, Allowing Below-Minimum Fee Rate Transactions Into the Pool - (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` in `tx-pool/src/util.rs` enforces `min_fee_rate` using only the serialized transaction size as the weight, while the canonical weight used everywhere else in the pool is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions, the two weights diverge by orders of magnitude. Because no post-verification fee rate re-check exists, an unprivileged submitter can pass the admission gate with a fee rate far below the configured `min_fee_rate`, saturating verification workers and degrading pool throughput for legitimate transactions.

## Finding Description
`check_tx_fee` (`tx-pool/src/util.rs` L28–54) computes the minimum required fee using only `tx_size`:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code comment explicitly acknowledges this is a "cheap check" using size directly. The canonical weight function `get_transaction_weight` (`util/types/src/core/tx_pool.rs` L298–303) computes `max(tx_size, cycles × 0.000_170_571_4)`, which is used by `TxEntry::fee_rate()` for eviction and block assembly scoring.

The full transaction processing path in `_process_tx` (`tx-pool/src/process.rs` L705–777) is:
1. `pre_check` → `check_tx_fee` (size-only, before cycles are known)
2. `verify_rtx` → actual cycles obtained
3. For local RPC submissions (`send_transaction`), `declared_cycles` is `None`, so the `DeclaredWrongCycles` guard at L736–749 is never triggered
4. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` creates the pool entry with actual cycles
5. No second fee rate check against `get_transaction_weight(tx_size, verified.cycles)` exists

The `send_transaction` RPC handler (`rpc/src/module/pool.rs` L612–634) calls `submit_local_tx` with no declared cycles, confirming the `declared_cycles = None` path is the standard local submission path.

The pool eviction trigger (`tx-pool/src/pool.rs` L298) is byte-based only:
```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```
A 200-byte transaction never triggers eviction regardless of its cycle consumption.

## Impact Explanation
This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

With default config (`min_fee_rate = 1000` shannons/KB, `max_tx_verify_cycles = TWO_IN_TWO_OUT_CYCLES × 20 ≈ 70M`):
- A 200-byte tx consuming 70M cycles requires only 200 shannons to pass `check_tx_fee`
- Its actual fee rate after admission: `200 × 1000 / 11940 ≈ 16 shannons/KB` — ~60× below `min_fee_rate`
- The verify queue accepts up to 256 MB of transaction data; at 200 bytes/tx, ~1.28M such transactions can be queued
- Each consumes 70M cycles of verification work, saturating `max_tx_verify_workers` and starving legitimate high-fee transactions from being verified and admitted

## Likelihood Explanation
The attack requires only an RPC call to `send_transaction` — no privileged access, no key material, no majority hashpower. The attacker must deploy a CKB-VM script that consumes near-`max_tx_verify_cycles` cycles (a tight loop), which is straightforward for any script author. The discrepancy grows linearly with cycles, so the attacker naturally targets the maximum cycle budget. The attack is repeatable across many UTXOs locked by the same script.

## Recommendation
After `verify_rtx` returns `verified.cycles`, add a post-verification fee rate check using the canonical two-dimensional weight before creating the `TxEntry`:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

For the relay path, `check_tx_fee` can additionally be updated to use declared cycles as a pre-verification estimate, closing the gap earlier.

## Proof of Concept
1. Deploy a CKB lock script on testnet that runs a tight loop consuming exactly `max_tx_verify_cycles - ε` cycles (e.g., 69,999,999 cycles).
2. Fund multiple UTXOs locked by that script.
3. For each UTXO, construct a transaction with serialized size ≈ 200 bytes and fee = `ceil(1000 × 200 / 1000)` = 200 shannons.
4. Submit each via `send_transaction` RPC. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200` shannons; each transaction passes.
5. After verification, each pool entry records `fee_rate ≈ 16 shannons/KB` — 60× below `min_fee_rate` — and is never evicted by the byte-based `limit_size` loop.
6. With enough UTXOs, the verify queue fills with cycle-heavy transactions, saturating verification workers and delaying or starving legitimate transactions.