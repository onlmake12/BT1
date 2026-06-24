Audit Report

## Title
Minimum Fee Check Uses Only Serialized Size, Ignoring Cycles — Allows Sub-Minimum Fee-Rate Transactions Into the Tx-Pool - (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size, while the actual in-pool fee rate is computed using `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A transaction with small serialized size but high cycle consumption passes the size-only gate and enters the pool with an effective fee rate far below the configured minimum, wasting node CPU and polluting the pool at a fraction of the intended cost.

## Finding Description
In `tx-pool/src/util.rs` lines 42–52, `check_tx_fee` computes the minimum required fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { ... }
```

The actual in-pool fee rate is computed in `tx-pool/src/component/entry.rs` lines 114–118 using the full weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

Where `get_transaction_weight` (`util/types/src/core/tx_pool.rs` lines 298–303) is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

The admission flow in `_process_tx` (`tx-pool/src/process.rs` lines 715–753) calls `pre_check` (which invokes `check_tx_fee`) **before** `verify_rtx`, so cycles are unknown at the fee-check point. After `verify_rtx` returns the actual cycle count, `TxEntry::new(rtx, verified.cycles, fee, tx_size)` is constructed and passed directly to `submit_entry` with no second fee-rate check against the full weight. The gap between the admission check and the actual resource cost is explicitly acknowledged in the code comment ("Theoretically we cannot use size as weight directly"), confirming the design is intentional but unmitigated.

## Impact Explanation
An attacker crafts a transaction with ~100 bytes serialized size and ~70,000,000 cycles (the `max_tx_verify_cycles` default). The size-only gate requires only ~100 shannons at 1,000 shannons/KB. The actual weight is `max(100, 70_000_000 × 0.000170571) ≈ 11,940`, giving an effective fee rate of ~8.4 shannons/KB — roughly 119× below the configured minimum. Each such transaction forces the node to execute the full cycle budget in `verify_rtx` before admission. An attacker with a moderate UTXO set can submit a stream of these transactions, consuming sustained CPU across all receiving nodes at a cost far below what the fee policy is intended to impose. This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, because the attacker's per-transaction cost is reduced by up to two orders of magnitude relative to the effective resource consumed.

## Likelihood Explanation
The path is fully reachable by any unprivileged caller via the `send_transaction` RPC or P2P relay. No special privileges, leaked keys, or victim mistakes are required. Crafting a small transaction with a high-cycle lock script is straightforward for any script author. The attack is repeatable as long as the attacker holds UTXOs, and each UTXO can be used to submit one such transaction. The code comment confirms the gap is known.

## Recommendation
After `verify_rtx` returns the actual cycle count and before calling `submit_entry`, perform a second fee-rate check using the full weight:

```rust
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
let actual_fee_rate = entry.fee_rate();
if actual_fee_rate < tx_pool_config.min_fee_rate {
    return Some((Err(Reject::LowFeeRate(tx_pool_config.min_fee_rate, ...)), snapshot));
}
```

This mirrors the pattern already used for eviction and sorting (`EvictKey`, `AncestorsScoreSortKey`) and closes the gap between the admission check and the actual resource cost.

## Proof of Concept
1. Deploy a lock script that loops for ~70,000,000 cycles but whose serialized witness is minimal (~100 bytes total transaction size).
2. Set the transaction fee to exactly `min_fee_rate.fee(100)` (e.g., 100 shannons at 1,000 shannons/KB).
3. Submit via `send_transaction` RPC.
4. Observe: `check_tx_fee` passes (fee ≥ size-based minimum, `tx-pool/src/util.rs` line 47).
5. `verify_rtx` executes the full cycle budget.
6. `TxEntry` is created with `verified.cycles ≈ 70_000_000`; actual weight ≈ 11,940; effective fee rate ≈ 8.4 shannons/KB.
7. No second check is performed; the entry is admitted via `submit_entry` (`tx-pool/src/process.rs` line 753).
8. Confirm via `get_pool_tx_detail_info` RPC: `score_sortkey.weight` >> `score_sortkey.fee / min_fee_rate`.
9. Repeat with additional UTXOs to sustain CPU load across nodes.