Audit Report

## Title
`check_tx_fee` Admission Uses Size-Only Fee Check While Eviction Uses Full Weight, Enabling Sub-Minimum Fee Rate Admission of Cycle-Heavy Transactions — (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` enforces the minimum fee rate using only serialized `tx_size`, while the actual transaction weight used for eviction and sorting is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A cycle-heavy, byte-light transaction can pass the admission gate at a fee rate orders of magnitude below the enforced minimum, forcing the node to execute full script verification (up to `max_block_cycles` = 70,000,000 cycles) before the discrepancy can be detected. No post-verification fee-rate check exists to close this gap.

## Finding Description
**Root cause:** In `tx-pool/src/util.rs` at L42–45, `check_tx_fee` explicitly uses only `tx_size` for the minimum fee calculation, with an inline comment acknowledging the approximation:

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

**Exploit flow:**

1. `_process_tx` (L705) calls `pre_check` (L715), which calls `check_tx_fee` at L289 with only `tx_size` — cycles are unknown at this point.
2. `verify_rtx` is then called (L724–732), executing full script verification up to `max_block_cycles`.
3. After verification, `TxEntry::new(rtx, verified.cycles, fee, tx_size)` is constructed at L751 with the actual cycles.
4. `submit_entry` (L96–170) is called with no fee-rate re-validation against the full weight.
5. `TxEntry::fee_rate()` in `entry.rs` at L115–117 correctly uses `get_transaction_weight(self.size, self.cycles)` for sorting and eviction — but this is too late; the expensive verification has already run.

**Why existing checks fail:**

- The `declared_cycles` check at L736–748 only rejects transactions where the declared value mismatches the verified value. For RPC submissions (`send_transaction`), `declared_cycles` is `None`, so `max_cycles = self.consensus.max_block_cycles()` (L720) — no declared-cycles rejection path applies.
- `limit_size` eviction at L151 triggers only when the pool is full and evicts lowest-fee-rate entries first (the attacker's own), but the expensive verification has already completed before eviction occurs.
- There is no second fee-rate check anywhere between L734 and L753 using `verified.cycles`.

## Impact Explanation
This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

With `min_fee_rate = 1000 shannons/KW`, `tx_size = 100 bytes`, `cycles = 70,000,000`:
- Fee required by `check_tx_fee`: `1000 × 100 / 1000 = 100 shannons`
- Actual weight: `max(100, 70,000,000 × 0.000_170_571_4) ≈ 11,940`
- Fee required by actual weight: `11,940 shannons`
- Effective admitted fee rate: `~8 shannons/KW` — approximately **119× below the enforced minimum**

Each such transaction forces the node's verification pipeline to execute 70,000,000 VM cycles at a cost of only 100 shannons. Sustained submission saturates the async verification queue and degrades node performance for legitimate transactions.

## Likelihood Explanation
- **Entry path:** Any unprivileged RPC caller (`send_transaction`) or P2P relayer can submit such a transaction. No special privilege is required.
- **Craft difficulty:** Low. The attacker needs a lock script executing a tight loop consuming ~70,000,000 cycles while keeping the serialized transaction small. The CKB-VM cycle limit and `DEFAULT_BYTES_PER_CYCLES` constant are publicly documented.
- **Cost:** ~119× cheaper than the intended minimum, making sustained submission economically viable.
- **Repeatability:** The attacker can continuously re-submit with distinct UTXOs. Pool eviction removes the attacker's low-fee-rate entries, but the attacker can immediately re-submit, keeping the verification pipeline saturated indefinitely.

## Recommendation
After verification completes and cycles are known, perform a second fee-rate check using the full weight before calling `submit_entry`. In `_process_tx`, between L734 and L751, add:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

Alternatively, in `check_tx_fee`, use `declared_cycles` (for relayed transactions) or `max_tx_verify_cycles` as a conservative upper bound to tighten the pre-check:

```rust
let weight = get_transaction_weight(tx_size, declared_cycles.unwrap_or(tx_pool.config.max_tx_verify_cycles));
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

The post-verification check is preferred as it uses exact cycles with no approximation.

## Proof of Concept
1. Craft a CKB transaction with a lock script executing a tight loop consuming ~70,000,000 cycles, minimal witnesses/outputs so `tx_size ≈ 100` bytes, and `fee = 100 shannons` (`min_fee_rate × tx_size / 1000`).
2. Submit via `send_transaction` RPC (no `declared_cycles` argument).
3. `check_tx_fee` computes `min_fee = 1000 × 100 / 1000 = 100 shannons`. Fee passes.
4. `verify_rtx` runs the full script: 70,000,000 VM cycles consumed.
5. `TxEntry` is created with `fee=100, cycles=70_000_000, size=100`.
6. `entry.fee_rate()` = `FeeRate::calculate(100, get_transaction_weight(100, 70_000_000))` ≈ **8 shannons/KW** — far below `min_fee_rate = 1000`.
7. Repeat with distinct UTXOs to continuously saturate the verification pipeline at 1/119th the intended cost.