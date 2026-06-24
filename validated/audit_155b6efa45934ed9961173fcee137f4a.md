Audit Report

## Title
Min Fee Rate Admission Check Uses Serialized Size While Post-Verification Weight Uses `max(size, cycles × factor)` — (`tx-pool/src/util.rs`)

## Summary
`check_tx_fee` gates pool admission using only the transaction's serialized byte size, but every downstream metric — block-assembly priority, eviction ordering, and per-entry fee rate — uses the true transaction weight (`max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`). Because cycles are unknown before script execution, no second weight-based fee check is performed after `verify_rtx` returns the actual cycle count. An attacker can craft a cycle-heavy, size-small transaction that passes the admission gate at a fraction of the intended minimum fee rate, allowing mempool flooding and CPU exhaustion at reduced cost.

## Finding Description
In `tx-pool/src/util.rs` lines 42–52, `check_tx_fee` explicitly uses only `tx_size` for the minimum fee calculation, with a comment acknowledging the limitation:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { return Err(Reject::LowFeeRate(...)); }
```

The true weight is defined in `util/types/src/core/tx_pool.rs` lines 298–303:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (≈ `MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES`).

In `tx-pool/src/process.rs` lines 705–777, the full processing flow is:
1. `pre_check` → `check_tx_fee` (size-only, line 289/294)
2. `verify_rtx` returns actual `verified.cycles` (line 734)
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` is created (line 751)
4. `submit_entry` is called (line 753)

**No second fee-rate check using the actual weight occurs between steps 2 and 4.** The `TxEntry` is admitted with the real cycle count, but the fee was only validated against `tx_size`. All subsequent uses of fee rate — `fee_rate()` (entry.rs:115–118), `AncestorsScoreSortKey` (entry.rs:221–231), `EvictKey` (entry.rs:234–247), and RPC fee-rate statistics (fee_rate.rs:97–106) — use `get_transaction_weight(size, cycles)`, creating a permanent inconsistency between the admission gate and the actual resource metric.

## Impact Explanation
This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

With `tx_size = 1 000 bytes`, `cycles = 70 000 000`, and `min_fee_rate = 1 000 shannons/KW`:
- Admission requires: `1 000 × 1 000 / 1 000 = 1 000 shannons`
- True weight-based requirement: `1 000 × 11 940 / 1 000 = 11 940 shannons`

The attacker pays ~8.4% of the intended minimum fee. At this ratio, the attacker can submit ~12× more transactions for the same cost as a legitimate user. Each submission forces the node to execute the full script verification (`verify_rtx`), consuming significant CPU. Even though the eviction mechanism (which correctly uses weight-based fee rate) will eventually remove these low-quality entries when the pool fills, the attacker can continuously re-submit, sustaining CPU exhaustion and mempool churn at a fraction of the intended economic cost. This degrades block-assembly quality and can cause network-wide congestion.

## Likelihood Explanation
The exploit path is fully unprivileged: any peer or RPC caller can invoke `send_transaction`. Constructing a transaction with high cycle consumption and small serialized size is straightforward — a lock script containing a tight loop with minimal witness/output data achieves this. No special keys, majority hashpower, or social engineering are required. The attack is repeatable and cheap to sustain.

## Recommendation
After `verify_rtx` returns the actual cycle count (line 734 of `process.rs`), perform a second fee-rate check using the true weight before creating the `TxEntry`:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(
        tx_pool_config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64()
    )), snapshot));
}
```

This requires passing `min_fee_rate` (or the pool config) into `_process_tx` after the lock is released, or performing the check inside `submit_entry` where the pool config is accessible.

## Proof of Concept
1. Construct a CKB transaction whose lock script executes a tight loop consuming ~70 000 000 cycles, with minimal outputs and witness (~1 000 serialized bytes).
2. Set the fee to exactly `min_fee_rate × tx_size` shannons (e.g., 1 000 shannons at the default 1 000 shannons/KW rate).
3. Submit via `send_transaction` RPC.
4. Observe the transaction is accepted (`check_tx_fee` passes using size-only check at `tx-pool/src/util.rs:45`).
5. Query `get_pool_transaction` — `TxEntry::fee_rate()` will report `fee / weight ≪ min_fee_rate`, confirming the inconsistency.
6. Repeat in a loop: each iteration forces full script execution on the node while paying ~8% of the intended minimum fee, sustaining CPU exhaustion and mempool churn.