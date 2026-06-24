Audit Report

## Title
Ineffective `min_fee_rate` Enforcement in `check_tx_fee` Using Serialized Size Instead of Weight — (`tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using raw serialized `tx_size` instead of the transaction's weight (`max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`). Since `FeeRate` is defined and documented as shannons per kilo-weight, using size produces a minimum that is always ≤ the weight-based minimum. An unprivileged submitter can craft a high-cycle, low-fee transaction that passes the size-based gate while its true weight-based fee rate is far below `min_fee_rate`, bypassing the pool's spam-protection threshold.

## Finding Description

**Root cause — `tx-pool/src/util.rs`, `check_tx_fee` (L28–54):**

The function accepts `tx_size: usize` and computes:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code itself contains a comment acknowledging the discrepancy:

> "Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly"

This is the **only** fee-rate admission gate. `check_tx_fee` is called in `pre_check` (L289, L294 of `tx-pool/src/process.rs`) before script verification, and there is no second weight-based fee check after verification completes.

**`FeeRate` is weight-based, not size-based:**

`util/types/src/core/fee_rate.rs` documents `FeeRate` as "shannons per kilo-weight" and `FeeRate::fee(weight)` computes `self.0 * weight / 1000`. Passing `tx_size` instead of `weight` directly misapplies the unit.

**Weight formula (`util/types/src/core/tx_pool.rs`, L298–303):**

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (L279). For a cycle-heavy transaction, `weight >> tx_size`.

**Correct usage exists elsewhere — `tx-pool/src/component/entry.rs`, L114–118:**

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

Pool ordering and eviction use weight correctly, but the admission gate does not.

**Exploit path:**

1. Attacker submits a transaction via `send_transaction` RPC with a script consuming ~70,000,000 cycles and a fee just above `min_fee_rate × tx_size / 1000`.
2. `pre_check` calls `check_tx_fee` with `tx_size` only — cycles are not yet known (pre-verification).
3. The size-based check passes; the transaction proceeds through script verification and enters the pool.
4. The transaction's actual weight-based fee rate is far below `min_fee_rate`.
5. Repeat in a loop to fill the pool.

**Concrete numbers (default config: `min_fee_rate = 1000`, `max_tx_verify_cycles = 70,000,000`):**

| Parameter | Value |
|---|---|
| `tx_size` | 597 bytes |
| `cycles` | 70,000,000 |
| `weight` | max(597, 70,000,000 × 0.000_170_571_4) = **11,940** |
| `min_fee_by_size` | 1000 × 597 / 1000 = **597 shannons** |
| `min_fee_by_weight` | 1000 × 11,940 / 1000 = **11,940 shannons** |
| Effective admitted fee rate | 597 / 11,940 × 1000 ≈ **50 shannons/KW** |

A **20× bypass** of the intended minimum fee rate.

## Impact Explanation

An unprivileged attacker can continuously flood the tx-pool (`max_tx_pool_size = 180 MB`) with economically underpriced transactions at ~5% of the intended minimum fee rate. This constitutes a low-cost denial-of-service against the tx-pool, degrading block-template quality and displacing legitimate transactions. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

- Requires no privilege: any `send_transaction` RPC caller or P2P relay peer can trigger this.
- Requires only a script that loops near `max_tx_verify_cycles` — a standard capability.
- The code comment explicitly acknowledges the gap, confirming it is a known but unmitigated design choice at the admission gate.
- Continuously resubmittable: eviction under pool pressure does not prevent re-entry.

## Recommendation

Replace `tx_size` with `weight` in `check_tx_fee`. Since `check_tx_fee` is called before script verification (cycles unknown), the check must be restructured: either perform a preliminary size-only check pre-verification and a definitive weight-based check post-verification, or pass the declared/cached cycles when available. At minimum, add a post-verification weight-based fee check:

```rust
use ckb_types::core::tx_pool::get_transaction_weight;

pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
    cycles: Cycle,
) -> Result<Capacity, Reject> {
    let fee = ...;
    let weight = get_transaction_weight(tx_size, cycles);
    let min_fee = tx_pool.config.min_fee_rate.fee(weight);
    if fee < min_fee {
        return Err(Reject::LowFeeRate(...));
    }
    Ok(fee)
}
```

This aligns the admission check with the fee-rate semantics used in sorting, eviction, and RBF throughout the pool.

## Proof of Concept

1. Construct a CKB transaction with a lock script that loops to consume ~70,000,000 cycles.
2. Set the transaction fee to `1000 × tx_size / 1000` shannons (e.g., 597 shannons for a 597-byte tx).
3. Submit via `send_transaction` RPC to a node with default config (`min_fee_rate = 1000`).
4. Observe the transaction is accepted into the pool (no `LowFeeRate` rejection).
5. Query `get_pool_tx_detail_info`; confirm the entry's weight-based fee rate (computed via `TxEntry::fee_rate()`) reports ~50 shannons/KW — far below the configured 1000 shannons/KW.
6. Repeat in a loop; observe pool fills with transactions whose true fee rate is ~20× below the intended minimum.