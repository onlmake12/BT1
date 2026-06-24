Audit Report

## Title
`min_fee_rate` Admission Bypass via High-Cycle Low-Size Transactions — (`File: tx-pool/src/util.rs`)

## Summary
`check_tx_fee` enforces the minimum fee rate using only the transaction's serialized byte size (`tx_size`), while the actual fee rate used for miner selection, pool eviction, and score sorting is computed via `get_transaction_weight(tx_size, cycles)` — which equals `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. When cycle consumption dominates, the weight can be up to ~60× larger than `tx_size`, allowing an attacker to admit transactions into the mempool at a fraction of the intended minimum fee cost. No post-verification weight-based fee check exists to close this gap.

## Finding Description
In `tx-pool/src/util.rs` (L42–45), `check_tx_fee` explicitly uses only `tx_size` for the minimum fee check, with a comment acknowledging the imprecision:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

This function is called from `pre_check` in `tx-pool/src/process.rs` (L274, L289) before script execution, when actual cycle consumption is unknown. After verification completes, `_process_tx` (L751) creates a `TxEntry` with the actual `verified.cycles` — but performs **no second fee check** using the weight metric. The entry's `fee_rate()` method in `tx-pool/src/component/entry.rs` (L115–117) then computes the true fee rate as:

```rust
let weight = get_transaction_weight(self.size, self.cycles);
FeeRate::calculate(self.fee, weight)
```

Where `get_transaction_weight` in `util/types/src/core/tx_pool.rs` (L298–303) is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

With `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (L279). For a transaction with `tx_size = 200` bytes and `cycles = 70_000_000`:
- **Admission check**: `min_fee = 1000 * 200 / 1000 = 200 shannons` → passes
- **Actual weight**: `max(200, 70_000_000 * 0.000_170_571_4) = 11,940`
- **Effective fee rate**: `200 * 1000 / 11,940 ≈ 16.7 shannons/KW` — ~60× below `min_fee_rate`

The gap is structural: `check_tx_fee` is the only fee-rate gate, and it uses a metric that diverges from the actual cost metric by up to 60× in the worst case.

## Impact Explanation
This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker can flood the 180 MB mempool (`max_tx_pool_size = 180_000_000`) with transactions whose effective fee rate is ~16.7 shannons/KW instead of the required 1000 shannons/KW, at ~60× lower fee cost than intended. These transactions occupy mempool space, are deprioritized by miners (sorted by weight-based fee rate), and are unlikely to be mined quickly — but they persist in the pool and can delay or evict legitimate transactions. The `min_fee_rate` invariant, which is the primary anti-spam mechanism for the mempool, is effectively bypassed for high-cycle transactions.

## Likelihood Explanation
- **Entry path**: Any unprivileged `send_transaction` RPC caller or P2P relay peer. No special privileges required.
- **Craft difficulty**: Low. A RISC-V tight loop consuming ~70M cycles can be encoded in a few dozen bytes of bytecode, deployed as a cell dep. The transaction itself (1 input, 1 output, 1 cell dep) serializes to ~200–300 bytes.
- **Cost**: ~200 shannons per transaction instead of ~11,940 shannons — a ~60× discount on admission cost.
- **Repeatability**: The attacker can submit many such transactions in parallel, limited by the 180 MB pool cap and `max_ancestors_count = 25`. Each transaction requires a distinct live input cell, but the fee savings make the attack economically viable at scale.

## Recommendation
Add a post-verification weight-based fee check in `_process_tx` (`tx-pool/src/process.rs`) after `verified.cycles` is known, before calling `submit_entry`:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

This is the most accurate fix (Option 2 from the report): it uses the actual execution metric and does not require cycles to be known at pre-check time. The existing size-only pre-check in `check_tx_fee` can remain as a cheap early filter, but the weight-based check after verification closes the gap.

## Proof of Concept
1. Deploy a CKB script cell containing a RISC-V tight loop that consumes ~70,000,000 cycles. The bytecode is ~20–50 bytes.
2. Create a transaction: 1 input (locked by the above script), 1 output, 1 cell dep pointing to the script cell. Serialized size ≈ 200–300 bytes.
3. Set fee = `ceil(min_fee_rate * tx_size / 1000)` = 200–300 shannons.
4. Submit via `send_transaction` RPC.
5. **Expected (correct)**: Rejected with `LowFeeRate` because effective fee rate ≈ 16.7 shannons/KW < 1000 shannons/KW.
6. **Actual**: Accepted. `check_tx_fee` passes because `fee >= min_fee_rate.fee(tx_size)`. The entry enters the pool with `fee_rate() ≈ 16.7 shannons/KW`.
7. Repeat with many distinct input cells to fill the 180 MB pool at ~60× lower cost than intended.

Verification: after submission, call `get_pool_transaction` and inspect the entry's fee vs. its weight-based fee rate to confirm the discrepancy.