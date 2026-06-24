Audit Report

## Title
Ineffective `min_fee_rate` Enforcement in `check_tx_fee` Using Serialized Size Instead of Weight — (`tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using raw serialized `tx_size` instead of transaction weight (`max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`). Since `FeeRate` is defined as shannons per kilo-weight, passing `tx_size` directly misapplies the unit and produces a minimum fee that is always ≤ the correct weight-based minimum. An unprivileged submitter can craft a high-cycle, low-fee transaction that passes the size-based admission gate while its true weight-based fee rate is far below `min_fee_rate`, enabling low-cost tx-pool flooding.

## Finding Description

**Root cause — `tx-pool/src/util.rs`, `check_tx_fee` (L28–54):**

The function signature accepts only `tx_size: usize` and computes:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The code comment at L42–44 explicitly acknowledges the discrepancy:

> "Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly"

This is the **only** fee-rate admission gate. `check_tx_fee` is called in `pre_check` at `tx-pool/src/process.rs` L289 and L294 before script verification, and there is no second weight-based fee check after verification completes (confirmed: grep for any post-verification weight-based fee check returns no matches). [2](#0-1) 

**`FeeRate` is weight-based, not size-based:**

`util/types/src/core/fee_rate.rs` documents `FeeRate` as "shannons per kilo-weight" and `FeeRate::fee(weight)` computes `self.0 * weight / 1000`. Passing `tx_size` instead of `weight` directly misapplies the unit. [3](#0-2) [4](#0-3) 

**Weight formula — `util/types/src/core/tx_pool.rs` (L298–303):**

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (L279). For a cycle-heavy transaction, `weight >> tx_size`. [5](#0-4) 

**Correct usage exists in pool ordering/eviction — `tx-pool/src/component/entry.rs` (L114–118):**

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

Pool ordering and eviction use weight correctly, but the admission gate does not. [6](#0-5) 

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

A **~20× bypass** of the intended minimum fee rate.

## Impact Explanation

An unprivileged attacker can continuously flood the tx-pool (`max_tx_pool_size = 180 MB`) with economically underpriced transactions at ~5% of the intended minimum fee rate. This constitutes a low-cost denial-of-service against the tx-pool, degrading block-template quality and displacing legitimate transactions. This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

- Requires no privilege: any `send_transaction` RPC caller or P2P relay peer can trigger this.
- Requires only a script that loops near `max_tx_verify_cycles` — a standard capability.
- The code comment at `tx-pool/src/util.rs` L42–44 explicitly acknowledges the gap, confirming it is a known but unmitigated design choice at the admission gate.
- Continuously resubmittable: eviction under pool pressure does not prevent re-entry. [7](#0-6) 

## Recommendation

Since `check_tx_fee` is called before script verification (cycles unknown), restructure the check into two stages: a preliminary size-only check pre-verification (as currently exists) and a definitive weight-based check post-verification once cycles are known. At minimum, add a post-verification weight-based fee check:

```rust
use ckb_types::core::tx_pool::get_transaction_weight;

pub(crate) fn check_tx_fee_with_weight(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
    cycles: Cycle,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        ...?;
    let weight = get_transaction_weight(tx_size, cycles);
    let min_fee = tx_pool.config.min_fee_rate.fee(weight);
    if fee < min_fee {
        return Err(Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64()));
    }
    Ok(fee)
}
```

This aligns the admission check with the fee-rate semantics used in `TxEntry::fee_rate()`, sorting, eviction, and RBF throughout the pool.

## Proof of Concept

1. Construct a CKB transaction with a lock script that loops to consume ~70,000,000 cycles.
2. Set the transaction fee to `1000 × tx_size / 1000` shannons (e.g., 597 shannons for a 597-byte tx).
3. Submit via `send_transaction` RPC to a node with default config (`min_fee_rate = 1000`).
4. Observe the transaction is accepted into the pool (no `LowFeeRate` rejection).
5. Query `get_pool_tx_detail_info`; confirm the entry's weight-based fee rate (computed via `TxEntry::fee_rate()` at `tx-pool/src/component/entry.rs` L114–118) reports ~50 shannons/KW — far below the configured 1000 shannons/KW.
6. Repeat in a loop; observe pool fills with transactions whose true fee rate is ~20× below the intended minimum.

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

**File:** tx-pool/src/process.rs (L288-295)
```rust
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
```

**File:** util/types/src/core/fee_rate.rs (L3-5)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** util/types/src/core/tx_pool.rs (L279-303)
```rust
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

/// vbytes has been deprecated, renamed to weight to prevent ambiguity
#[deprecated(
    since = "0.107.0",
    note = "Please use the get_transaction_weight instead"
)]
pub fn get_transaction_virtual_bytes(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}

/// The miners select transactions to fill the limited block space which gives the highest fee.
/// Because there are two different limits, serialized size and consumed cycles,
/// the selection algorithm is a multi-dimensional knapsack problem.
/// Introducing the transaction weight converts the multi-dimensional knapsack to a typical knapsack problem,
/// which has a simple greedy algorithm.
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
