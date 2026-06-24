Audit Report

## Title
`check_tx_fee` Minimum Fee Rate Admission Gate Uses Serialized Size Only, Ignoring Cycles — Cycle-Heavy Transactions Bypass the Threshold - (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` enforces the minimum fee rate using only the transaction's serialized byte size as the weight denominator, while the rest of the pool uses `get_transaction_weight(tx_size, cycles)` — which is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)` — for all fee-rate ranking and eviction decisions. For cycle-heavy transactions the two values diverge by up to ~119×. There is no post-verification fee rate re-check using the actual cycle count, so the admission gate is effectively inoperative for that class of transaction. An unprivileged caller can submit transactions whose actual fee rate is orders of magnitude below the configured `min_fee_rate`.

## Finding Description

`check_tx_fee` in `tx-pool/src/util.rs` (lines 42–52) explicitly uses `tx_size` as the weight argument to `min_fee_rate.fee()`, with a comment acknowledging the theoretical incorrectness:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The actual transaction weight used everywhere else in the pool is defined in `util/types/src/core/tx_pool.rs` (lines 298–303):

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

with `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (line 279).

The full transaction processing path in `_process_tx` (`tx-pool/src/process.rs`, lines 705–777) is:

1. `pre_check` → calls `check_tx_fee(tx_pool, snapshot, rtx, tx_size)` — size-only gate.
2. `verify_rtx` → returns `Completed { cycles, fee }` with the actual cycle count.
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry is created with real cycles.
4. `submit_entry` — no fee rate re-check against actual weight.

There is no second call to `check_tx_fee` or any equivalent check after `verify_rtx` returns the real cycle count. The `fee` value carried forward is the one computed before cycles are known, and it is never re-validated against `get_transaction_weight(tx_size, verified.cycles)`.

Inside the pool, `TxEntry::fee_rate()` (`tx-pool/src/component/entry.rs`, lines 114–118) correctly uses `get_transaction_weight`:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

This is used for sorting and eviction (`EvictKey`, `AncestorsScoreSortKey`), but only after the transaction has already been admitted.

**Concrete arithmetic** with `min_fee_rate = 1,000 shannons/KW`, `max_tx_verify_cycles = 70,000,000`, `tx_size ≈ 200 bytes`:

- Size-based gate requires: `1,000 × 200 / 1,000 = 200 shannons` → **passes**.
- Actual weight: `max(200, 70,000,000 × 0.000_170_571_4) ≈ 11,940`.
- Actual fee rate: `200 × 1,000 / 11,940 ≈ 16 shannons/KW` — ~62× below the 1,000 shannons/KW threshold.

## Impact Explanation

An attacker can continuously submit cycle-heavy, byte-small transactions that pass the size-based admission gate while carrying an actual fee rate far below `min_fee_rate`. These transactions:

1. Enter the pool legitimately (no `LowFeeRate` rejection).
2. Are ranked near the bottom of the fee-rate queue, so miners deprioritize them.
3. Occupy pool memory (up to the 180 MB `max_tx_pool_size` cap) until eviction or expiry (default 12 hours).

Sustained submission fills the pool with sub-threshold entries, degrading pool quality and crowding out legitimate transactions. The eviction mechanism (`limit_size`) does use the actual weight-based fee rate, so these transactions are evicted first when the pool is full — but only after they have already displaced legitimate transactions. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, because the attacker pays 60–125× less fee than the configured threshold requires.

## Likelihood Explanation

The attack requires a valid RPC connection or peer relay path and valid UTXOs to spend. No privileged access, key material, or majority hashpower is needed. The attacker must deploy or reference a high-cycle script (e.g., a type script consuming close to `max_tx_verify_cycles = 70,000,000` cycles), which requires an on-chain cell dep but is a one-time setup cost. Each subsequent attack transaction pays only the size-based minimum fee (e.g., 200 shannons for a 200-byte transaction) rather than the cycle-weight-based minimum (~11,940 shannons). The `max_tx_verify_cycles` limit (70 M) is the only natural bound on the discrepancy. The attack is repeatable as long as the attacker has UTXOs to spend.

## Recommendation

After `verify_rtx` returns the actual cycle count in `_process_tx`, re-run the fee rate check using the true weight before creating the `TxEntry`:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let actual_min_fee = tx_pool_config.min_fee_rate.fee(actual_weight);
if fee < actual_min_fee {
    return Some((Err(Reject::LowFeeRate(
        tx_pool_config.min_fee_rate,
        actual_min_fee.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

The pre-check in `check_tx_fee` can remain as a cheap early-exit for obviously under-fee'd transactions; the post-verification check becomes the authoritative gate.

## Proof of Concept

1. Deploy a type script on-chain that loops until it consumes ≈ 70,000,000 cycles.
2. Craft a transaction with:
   - Serialized size ≈ 200 bytes (minimal lock script, one input, one output).
   - The above type script as a cell dep.
   - Fee = `min_fee_rate × tx_size / 1000 = 1,000 × 200 / 1,000 = 200 shannons`.
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = fee(200) = 200 shannons`. Fee ≥ min_fee → **accepted**.
5. `verify_rtx` runs the script, returning `verified.cycles ≈ 70,000,000`.
6. No post-verification fee rate check is performed. `TxEntry::new(rtx, 70_000_000, 200_shannons, 200)` is created and submitted.
7. Inside the pool, `entry.fee_rate()` computes `FeeRate::calculate(200, 11940) ≈ 16 shannons/KW` — 62× below the 1,000 shannons/KW threshold.
8. The transaction sits in the pool ranked near the bottom for up to 12 hours. Repeat with different UTXOs to fill the pool with sub-threshold entries.