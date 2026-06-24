Audit Report

## Title
Minimum Fee Rate Check Uses Only Serialized Size, Ignoring Cycle-Based Weight — (`tx-pool/src/util.rs`)

## Summary
`check_tx_fee` enforces the minimum fee rate using only the transaction's serialized byte size (`tx_size`), while the actual transaction weight used for pool ordering and eviction is `get_transaction_weight(tx_size, cycles) = max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. An unprivileged sender can craft a compact script that consumes near-maximum VM cycles, causing the transaction to pass the minimum fee gate while its true weight-based effective fee rate is up to ~60× below the configured minimum. The code comment at the root cause site explicitly acknowledges this as a known theoretical gap.

## Finding Description
In `tx-pool/src/util.rs` (L42–52), `check_tx_fee` computes the minimum required fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

This check runs inside `pre_check` (process.rs L269–316), before VM execution. Cycles are not yet known at this point. After `pre_check` passes, `verify_rtx` runs and returns `verified.cycles`, which are then stored in the `TxEntry` (process.rs L751). From that point on, all pool ordering and eviction uses the true weight:

```rust
// entry.rs L114-118
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

where `get_transaction_weight` (tx_pool.rs L298–303) is `max(tx_size, cycles * 0.000_170_571_4)`.

With default config (`min_fee_rate = 1_000` shannons/KB, `max_tx_verify_cycles = 70_000_000`):

| Parameter | Value |
|---|---|
| `tx_size` | ~300 bytes |
| `cycles` | ~69,000,000 |
| `weight` | `max(300, 11,769)` = **11,769** |
| `min_fee` (size-only check) | `1,000 × 300 / 1,000` = **300 shannons** |
| Effective fee rate | `300 × 1,000 / 11,769` ≈ **25 shannons/KB** |
| Ratio vs. minimum | **~40× below minimum** |

The transaction is admitted to the pool. The pool capacity (`max_tx_pool_size = 180 MB`) is tracked in serialized bytes (`pool_map.total_tx_size`, pool_map.rs L69), not weight. A 300-byte transaction contributes only 300 bytes to this counter, so the attacker can pack ~600,000 such transactions into the pool (180 MB / 300 bytes), each consuming ~70 M cycles of verification work, at a total cost of ~1.8 CKB.

## Impact Explanation
This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

Each admitted transaction triggers full VM script execution (up to 70 M cycles). Flooding the pool with 600,000 such transactions saturates the verification worker threads and the verify queue, degrading transaction relay and block assembly for all nodes. Legitimate transactions are not immediately evicted (the eviction mechanism correctly uses weight-based fee rate, so attacker transactions are evicted first), but the sustained CPU load from verification and the pool occupancy degrade node performance and network throughput at a cost far below what the `min_fee_rate` is intended to impose.

## Likelihood Explanation
The attack requires no privileged access:
1. Deploy a compact RISC-V loop script on-chain (one-time, trivial cost).
2. Submit transactions referencing this script via the standard `send_transaction` RPC or P2P relay.
3. No victim mistakes or external dependencies required.

The code comment at the root cause site confirms this is a known design gap, not an oversight. The attack is repeatable and automatable.

## Recommendation
After `verify_rtx` returns `verified.cycles`, perform a second fee check using the true weight before admitting the entry to the pool:

```rust
// In _process_tx, after verify_rtx:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

Alternatively, apply a conservative upper-bound estimate at `pre_check` time using `max_tx_verify_cycles` as a stricter admission gate, then refine after verification.

## Proof of Concept
1. Compile a minimal RISC-V loop script that executes ~69,000,000 cycles. The binary is ~150–300 bytes.
2. Deploy the script cell on-chain.
3. Construct a transaction with this script as a lock; `tx_size ≈ 300 bytes`.
4. Set `fee = 300 shannons` (= `min_fee_rate × tx_size / 1000 = 1,000 × 300 / 1,000`).
5. Submit via `send_transaction` RPC.
6. `check_tx_fee` passes: `300 ≥ 300`.
7. After VM execution: `verified.cycles ≈ 69,000,000`; `weight = max(300, 11,769) = 11,769`; effective fee rate ≈ 25 shannons/KB — ~40× below the 1,000 shannons/KB minimum.
8. Transaction is admitted. Repeat in a loop to fill the pool and saturate verification workers.