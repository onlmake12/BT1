Audit Report

## Title
Fee Rate Minimum Check Uses Serialized Size Instead of Transaction Weight, Allowing Cycle-Heavy Transactions to Bypass the Minimum Fee Rate — (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size as the weight argument to `FeeRate::fee`, while CKB's canonical transaction weight is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions, the true weight can be ~12× larger than the byte size. No subsequent weight-based fee check is performed after `verify_rtx` returns the actual cycle count, meaning a transaction submitter can craft a cycle-heavy transaction whose fee satisfies the size-based check but is far below the minimum fee rate when measured against the actual weight.

## Finding Description
`FeeRate` is defined as shannons per kilo-weight, where weight is `max(tx_size_bytes, cycles × DEFAULT_BYTES_PER_CYCLES)` per `get_transaction_weight` in `util/types/src/core/tx_pool.rs`.

`check_tx_fee` at `tx-pool/src/util.rs:45` calls `tx_pool.config.min_fee_rate.fee(tx_size as u64)`, substituting raw byte size for weight. The developer comment on lines 42–44 explicitly acknowledges this: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check."*

The full admission path in `_process_tx` (`tx-pool/src/process.rs:705–777`) is:
1. `pre_check` (line 715) → calls `check_tx_fee` with `tx_size` only (lines 289, 294)
2. `verify_rtx` (lines 724–732) → returns `verified` containing the actual cycle count
3. Lines 736–749: only checks declared vs. verified cycles for remote txs
4. Line 751: `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — no weight-based fee check
5. `submit_entry` (line 753) — transaction is admitted

There is no second fee-rate check using `get_transaction_weight(tx_size, verified.cycles)` anywhere after `verify_rtx` returns. The same pattern applies in `readd_detached_tx` (lines 889–913). By contrast, the RPC fee-rate statistics path at `rpc/src/util/fee_rate.rs` correctly calls `get_transaction_weight(*size as usize, cycles)`, creating a split: the admission gate uses size; the statistics path uses weight.

## Impact Explanation
This matches the allowed CKB bounty impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

With default `min_fee_rate = 1000` shannons/KW and `max_tx_verify_cycles = 70_000_000`:
- A 1,000-byte tx consuming 70M cycles has weight = `max(1000, 70_000_000 × 0.000_170_571_4)` = **11,940**
- Fee required by `check_tx_fee`: `1000 × 1000 / 1000` = **1,000 shannons**
- Fee required at true weight: `1000 × 11,940 / 1000` = **11,940 shannons**
- Effective fee rate: `1000 × 1000 / 11940 ≈ 84` shannons/KW — **~11.9× below the configured minimum**

An attacker can flood the mempool with cycle-heavy, underpriced transactions at ~1/12 the intended cost, degrading node performance and displacing legitimately priced transactions.

## Likelihood Explanation
- Requires only a valid CKB transaction with a script consuming many cycles but a small serialized body — a normal property of any non-trivial lock or type script.
- No privileged access, key material, or majority hashpower is needed.
- The attacker controls both the script (cycle count) and the fee (capacity delta), making the exploit fully parameterizable.
- `max_tx_verify_cycles = 70_000_000` sets a hard upper bound, so the maximum weight amplification factor is bounded but still ~12×.
- Fully repeatable; the attacker can continuously submit such transactions.

## Recommendation
After `verify_rtx` returns the actual cycle count in `_process_tx`, perform a second fee-rate check using the true weight:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(tx_pool_config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64())), snapshot));
}
```

Alternatively, modify `check_tx_fee` to accept an optional declared cycle count and apply a conservative upper-bound estimate before script execution so that a single check suffices.

## Proof of Concept
1. Craft a CKB transaction whose lock script runs a tight loop consuming ~70,000,000 cycles. Keep the serialized transaction body small (e.g., 1,000 bytes).
2. Set the fee to exactly `min_fee_rate × tx_size / 1000 = 1000 × 1000 / 1000 = 1,000` shannons.
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = FeeRate(1000).fee(1000) = 1000 shannons`; the fee equals the threshold, so the transaction passes `pre_check`.
5. `verify_rtx` executes the script and returns `verified.cycles ≈ 70,000,000`; no subsequent weight-based fee check is performed.
6. The transaction is admitted. Its actual weight is `max(1000, 70,000,000 × 0.000_170_571_4) = 11,940`; effective fee rate ≈ 84 shannons/KW — well below the 1,000 shannons/KW minimum.
7. Repeat to fill the mempool with cycle-heavy, underpriced transactions.