Audit Report

## Title
`check_tx_fee` Uses Raw `tx_size` Without Cycles Normalization, Allowing Sub-Minimum Fee Rate Admission — (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` enforces the minimum fee rate using only the serialized byte size of a transaction, while the actual transaction weight used for eviction and sorting is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A cycle-heavy, byte-light transaction can pass the admission gate paying only the size-based minimum fee — orders of magnitude below the weight-based minimum — forcing the node to run expensive script verification for nearly-free, and occupying pool space at a steep discount. The code itself acknowledges this is a known approximation via an inline comment, but the security consequence of the gap is unaddressed.

## Finding Description
In `tx-pool/src/util.rs` at L42–45, `check_tx_fee` explicitly uses only `tx_size` for the minimum fee calculation, with a comment acknowledging the limitation:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The actual weight function in `util/types/src/core/tx_pool.rs` at L298–303 is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

The flow in `tx-pool/src/process.rs` confirms the gap:
- `pre_check` (L289) calls `check_tx_fee` with `tx_size` before cycles are known.
- After verification, `_process_tx` (L751) constructs `TxEntry::new(rtx, verified.cycles, fee, tx_size)` and calls `submit_entry` with no second fee-rate check.
- `submit_entry` (L96–170) performs no fee-rate re-validation against the full weight.
- `TxEntry::fee_rate()` in `tx-pool/src/component/entry.rs` at L115–117 correctly uses `get_transaction_weight(self.size, self.cycles)` for sorting and eviction — creating a split between the admission gate (size-only) and the eviction/sorting logic (full weight).

The critical consequence is that the node is forced to run full script verification (up to `max_block_cycles` = 70,000,000 cycles) for a transaction that paid only the size-based minimum fee, before it can determine the actual cycles. This is the expensive step that cannot be short-circuited.

## Impact Explanation
This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

With `min_fee_rate = 1000 shannons/KW`, `tx_size = 100 bytes`, and `cycles = 70,000,000`:
- Fee required by `check_tx_fee`: `1000 × 100 / 1000 = 100 shannons`
- Actual weight: `max(100, 70,000,000 × 0.000_170_571_4) = 11,940`
- Fee required by actual weight: `11,940 shannons`
- Effective fee rate admitted: `~8 shannons/KW` — **~119× below the enforced minimum**

Each such transaction forces the node's verification pipeline to execute 70,000,000 VM cycles at a cost of only 100 shannons. An attacker with a modest UTXO set can continuously submit such transactions, saturating the async verification queue and degrading node performance for legitimate transactions.

## Likelihood Explanation
- **Entry path**: Any unprivileged RPC caller (`send_transaction`) or P2P relayer can submit such a transaction. No special privilege is required.
- **Craft difficulty**: Low. The attacker needs a lock script that executes a tight loop consuming ~70,000,000 cycles while keeping the serialized transaction small. The CKB-VM cycle limit is publicly documented.
- **Cost**: ~119× cheaper than the intended minimum, making sustained submission economically viable.
- **Repeatability**: The attacker can continuously re-submit as long as they hold valid UTXOs. The `limit_size` eviction only triggers when the pool is full and evicts lowest-fee-rate entries first — which are the attacker's own transactions — but the attacker can immediately re-submit, keeping the verification pipeline saturated.

## Recommendation
After verification completes and cycles are known, perform a second fee-rate check using the full weight before calling `submit_entry`. Alternatively, in `check_tx_fee`, use `declared_cycles` (for relayed transactions) or `max_tx_verify_cycles` as a conservative upper bound:

```rust
let weight = get_transaction_weight(tx_size, declared_cycles.unwrap_or(tx_pool.config.max_tx_verify_cycles));
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

A post-verification check in `_process_tx` (after L734, before L751) using `verified.cycles` would close the gap with certainty and no approximation.

## Proof of Concept
1. Craft a CKB transaction with a lock script executing a tight loop consuming ~70,000,000 cycles, minimal witnesses/outputs so `tx_size ≈ 100` bytes, and `fee = 100 shannons` (`min_fee_rate × tx_size / 1000`).
2. Submit via `send_transaction` RPC or P2P relay.
3. `check_tx_fee` computes `min_fee = 1000 × 100 / 1000 = 100 shannons`. Fee passes.
4. `verify_rtx` runs the full script: 70,000,000 VM cycles consumed.
5. `TxEntry` is created with `fee=100, cycles=70_000_000, size=100`.
6. `entry.fee_rate()` = `FeeRate::calculate(100, get_transaction_weight(100, 70_000_000))` = `100 × 1000 / 11_940` ≈ **8 shannons/KW** — far below `min_fee_rate = 1000`.
7. Repeat with distinct UTXOs to continuously saturate the verification pipeline at 1/119th the intended cost.