Audit Report

## Title
`check_tx_fee` Admission Uses `tx_size`-Only Fee Check While Pool Prioritization Uses `get_transaction_weight(size, cycles)` — (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` enforces `min_fee_rate` using only the serialized transaction byte size (`tx_size`) as a deliberate "cheap pre-check" before script execution. After `verify_rtx` returns the actual cycle count, a `TxEntry` is created and inserted into the pool with no second fee-rate validation against the true weight (`max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`). An attacker can craft a small-serialized-size, high-cycle transaction that passes admission with a near-zero fee while consuming significant CPU and occupying pool space at an effective fee rate far below `min_fee_rate`.

## Finding Description
In `tx-pool/src/util.rs` L42–52, `check_tx_fee` explicitly uses `tx_size` only, with a comment acknowledging the limitation:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

This is called from `pre_check` in `_process_tx` (`process.rs` L715–717) before `verify_rtx` runs. After verification completes at L724–734, the actual cycle count is known. At L751–753, a `TxEntry` is created and passed to `submit_entry` with no subsequent fee-rate check:

```rust
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
```

Once in the pool, `TxEntry::fee_rate()` (`entry.rs` L115–118) and pool sorting/eviction (`entry.rs` L221–247) use the true weight via `get_transaction_weight(size, cycles)` = `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. The gap between the admission metric (size) and the pool metric (weight) is never reconciled.

## Impact Explanation
This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** An attacker paying fees proportional only to `tx_size` (e.g., 201 shannons for a 200-byte transaction at 1000 shannons/KW) can force the node to execute scripts consuming up to `max_block_cycles` cycles — the default cap used for local RPC submissions (`max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles())`). The effective fee rate after verification can be ~60× below `min_fee_rate`. Repeated submissions waste CPU on expensive script verification and fill the pool with entries that will only be evicted after displacing legitimate higher-fee-rate transactions.

## Likelihood Explanation
The `send_transaction` RPC endpoint requires no special privileges. Constructing a CKB transaction with a small serialized body (minimal inputs/outputs, script referenced by `code_hash`) but a lock script running a tight loop consuming tens of millions of cycles is straightforward using the CKB-VM toolchain. The attacker pays only the size-based minimum fee per submission, making this a low-cost, repeatable attack.

## Recommendation
After `verify_rtx` returns the actual cycle count and before calling `submit_entry`, perform a second fee-rate check using the true weight:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(
        tx_pool.config.min_fee_rate,
        min_fee_by_weight.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

This mirrors the pattern already used in `TxEntry::fee_rate()` and closes the gap between the admission check and the actual pool prioritization metric.

## Proof of Concept
1. Write a CKB lock script that executes a tight loop consuming ~70,000,000 cycles. Deploy it on a testnet cell.
2. Construct a transaction referencing that cell as a lock, keeping the serialized transaction body small (~200 bytes).
3. Set the transaction fee to 201 shannons (just above `min_fee_rate.fee(200) = 200` at 1000 shannons/KW).
4. Submit via `send_transaction` RPC.
5. Observe: `check_tx_fee` passes (201 > 200). `verify_rtx` executes the script, consuming ~70M cycles of CPU.
6. After insertion, query the pool entry: `fee_rate()` = `201 / max(200, 70_000_000 × 0.000170571) × 1000` ≈ `201 / 11,940 × 1000` ≈ **16.8 shannons/KW** — ~60× below `min_fee_rate`.
7. Repeat to fill the pool with such entries, displacing legitimate transactions and continuously burning node CPU.