Audit Report

## Title
`check_tx_fee` Enforces Minimum Fee Against Byte Size Only, Not Cycle-Adjusted Weight — (`tx-pool/src/util.rs`)

## Summary

`check_tx_fee` computes the minimum required fee using only the transaction's serialized byte size, while the actual fee rate used for pool sorting and eviction is computed against `get_transaction_weight(size, cycles)` — the maximum of byte size and cycle-adjusted size. An attacker can craft a cycle-heavy, byte-small transaction that passes the admission check while paying a fee rate far below `min_fee_rate`, causing the node to perform expensive script verification without adequate compensation. The code comment at the check site explicitly acknowledges this is a "cheap check" using size only, confirming the design gap is known but unmitigated.

## Finding Description

In `tx-pool/src/util.rs` (L42–45), `check_tx_fee` computes the minimum fee as:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

This uses raw serialized bytes as the sole weight denominator. However, the actual fee rate for every pool entry is computed via `get_transaction_weight` in `util/types/src/core/tx_pool.rs` (L298–303):

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (L279), derived from `MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES = 597_000 / 3_500_000_000`.

`TxEntry::fee_rate()` in `tx-pool/src/component/entry.rs` (L114–118) uses this weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

The exploit path in `_process_tx` (`tx-pool/src/process.rs` L715–732) is:
1. `pre_check` → `check_tx_fee` (byte-size check only, passes)
2. `verify_rtx` → `ContextualTransactionVerifier::verify(max_cycles)` (full script execution, expensive)
3. `TxEntry` is created with actual cycles; `fee_rate()` now reflects the true (low) effective rate

For local RPC submissions, `declared_cycles` is `None`, so `max_cycles = self.consensus.max_block_cycles()` = 3,500,000,000 cycles — not the 70M `max_tx_verify_cycles` limit. The per-transaction CPU exposure is therefore larger than the claim states.

For P2P relay, `declared_cycles` must match actual cycles post-verification (`DeclaredWrongCycles` check at L736–749), but `check_tx_fee` still uses byte size only, so the fee undercharge still applies before verification runs.

## Impact Explanation

This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

A transaction with 500 bytes and 70M cycles passes `check_tx_fee` paying 500 shannons (1,000 × 500 / 1,000). Its actual weight is `max(500, 11,940) = 11,940`, giving an effective fee rate of ~42 shannons/KB — 24× below the 1,000 shannons/KB minimum. For local RPC with no declared cycles, the node runs up to 3.5B cycles of CKB-VM execution per transaction. An attacker can keep the verification worker threads saturated at negligible cost, degrading transaction processing throughput across the node and, if coordinated across multiple nodes, across the network.

## Likelihood Explanation

Any unprivileged caller with access to `send_transaction` (local RPC) or any connected P2P peer can trigger this. Crafting a cycle-heavy, byte-small transaction requires only a lock script that runs a tight loop — no special privileges or victim interaction needed. The verify queue's `Full` rejection provides a soft bound but does not prevent the attacker from keeping the queue perpetually saturated. The attack is repeatable and economically viable at ~500 shannons per multi-billion-cycle verification run.

## Recommendation

Replace the byte-size-only check in `check_tx_fee` with a weight-aware check:

1. If the caller declares cycles (already supported via the `cycles` field in `send_transaction` and the `declared_cycles` parameter in `_process_tx`), pass `declared_cycles` into `check_tx_fee` and compute `min_fee` against `get_transaction_weight(tx_size, declared_cycles)`.
2. If no cycles are declared (local RPC path), apply a conservative floor using `max_tx_verify_cycles * DEFAULT_BYTES_PER_CYCLES` as the minimum weight, ensuring the fee covers worst-case verification cost before any script execution occurs.

## Proof of Concept

1. Construct a CKB transaction with a lock script that executes a tight loop consuming ~70M cycles. Keep the serialized transaction size at ~500 bytes by minimizing witness and output data.
2. Call `send_transaction` via local RPC with this transaction, paying a fee of 500 shannons (passes `check_tx_fee`: `1000 * 500 / 1000 = 500`).
3. Observe the node runs `verify_rtx` → `ContextualTransactionVerifier::verify` consuming up to 70M (or up to 3.5B for no-declared-cycles path) CKB-VM cycles.
4. The transaction is admitted with an effective fee rate of ~42 shannons/KB, far below the 1,000 shannons/KB minimum.
5. Repeat in a loop to saturate the verification worker pool. Monitor CPU usage on the node to confirm exhaustion of verification capacity at a cost of ~500 shannons per iteration.