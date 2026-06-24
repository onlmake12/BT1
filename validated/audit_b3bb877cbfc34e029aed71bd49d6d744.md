Audit Report

## Title
Tx-Pool Admission and RBF Fee Check Uses Serialized Size Only, Allowing High-Cycle Transactions to Underpay Fees — (File: `tx-pool/src/util.rs`, `tx-pool/src/pool.rs`)

## Summary

The pool admission check (`check_tx_fee`) and the RBF minimum replacement fee calculation (`calculate_min_replace_fee`) both compute required fees using only the transaction's serialized byte size, while the rest of the pool (mining priority, eviction, fee rate statistics) uses `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy, byte-small transactions, the weight can be ~60× larger than the size alone. This allows any unprivileged submitter to enter the pool — or replace an existing transaction — while paying a fee far below the actual resource cost, enabling pool flooding at a significant discount and undermining RBF economic guarantees.

## Finding Description

**Pool admission (`check_tx_fee`):**

In `tx-pool/src/util.rs` at line 45, the minimum fee is computed as:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code itself acknowledges the discrepancy with the comment at lines 42–44:
> "Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly" [1](#0-0) 

This is called from `pre_check` in `tx-pool/src/process.rs` at lines 289 and 294, before verification runs and before cycles are known. [2](#0-1) 

After verification, `TxEntry::fee_rate()` in `tx-pool/src/component/entry.rs` correctly uses `get_transaction_weight`:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [3](#0-2) 

So the pool admits the transaction at a size-only fee, but scores it for mining/eviction using the correct (much larger) weight — the transaction has a very low effective fee rate and will be deprioritized or evicted, but only after consuming pool resources.

For a transaction with `size = 200 bytes` and `cycles = 70,000,000` (default `max_tx_verify_cycles`):
- Actual weight: `max(200, 70_000_000 × 0.000_170_571_4) ≈ 11,940`
- Fee required by admission check: `min_fee_rate × 200 / 1000`
- Fee that should be required: `min_fee_rate × 11,940 / 1000`
- **Discount factor: ~59.7×** [4](#0-3) 

**RBF replacement fee (`calculate_min_replace_fee`):**

In `tx-pool/src/pool.rs` at line 103, the extra RBF fee is:

```rust
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
``` [5](#0-4) 

This function is called from `submit_entry`, which runs *after* verification — at which point cycles are known. There is no design constraint preventing the use of `get_transaction_weight(size, cycles)` here. A replacement transaction with high cycles but small serialized size pays a disproportionately small extra RBF fee, allowing it to displace an existing transaction while paying less than the actual resource cost of the replacement.

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker can craft transactions with minimal serialized size but near-maximum cycles. These pass the size-only admission check at up to ~60× discount relative to the intended minimum fee rate. The pool fills with low-effective-fee-rate transactions that consume block cycle budget disproportionately. While the eviction mechanism will eventually remove them, the attacker can continuously resubmit at reduced cost, sustaining pool pressure. The RBF case additionally allows displacement of legitimate transactions by paying less than the intended minimum replacement cost, undermining the economic guarantees of the RBF mechanism.

## Likelihood Explanation

**Medium.** The entry path is fully open to any unprivileged tx-pool submitter via the `send_transaction` RPC or P2P relay. The attacker needs to deploy a CKB script that consumes many cycles (e.g., a lock script with a tight loop), which is a standard capability of any CKB script author. The discrepancy is largest when `cycles` approaches `max_tx_verify_cycles` (default 70,000,000), a publicly known and reachable limit. No privileged access, key material, or majority hashpower is required. The attack is repeatable and cheap relative to the pool disruption caused.

## Recommendation

For `check_tx_fee`: since cycles are not known at pre-check time, use the declared cycles (when provided by the submitter via P2P relay) or `max_tx_verify_cycles` as a conservative upper bound:

```rust
// Instead of:
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);

// Use:
let weight = get_transaction_weight(tx_size, declared_cycles_or_max);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

For `calculate_min_replace_fee`: cycles are available at call time (post-verification). Update the function signature to accept `cycles: u64` and replace:

```rust
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
// with:
let weight = get_transaction_weight(size, cycles);
let extra_rbf_fee = self.config.min_rbf_rate.fee(weight);
```

## Proof of Concept

**Pool admission fee evasion:**

1. Deploy a CKB lock script that consumes ~70,000,000 cycles (e.g., a tight loop).
2. Craft a transaction spending a cell locked by this script, with minimal outputs (small serialized size, e.g., 200 bytes).
3. Set the transaction fee to `min_fee_rate × 200 / 1000` (at default `min_fee_rate = 1000`, this is 200 shannons).
4. Submit via `send_transaction` RPC.
5. `check_tx_fee` in `pre_check` computes `min_fee = 1000 × 200 / 1000 = 200 shannons` and accepts the transaction.
6. After verification, `TxEntry::fee_rate()` computes `weight = max(200, 70_000_000 × 0.000_170_571_4) ≈ 11,940` and the effective fee rate is `200 × 1000 / 11,940 ≈ 16 shannons/KW` — far below the `min_fee_rate` of 1000 shannons/KW.
7. Repeat continuously to flood the pool with low-effective-fee-rate transactions at ~60× cost discount.

**RBF fee evasion:**

1. Submit a legitimate transaction T1 to the pool.
2. Craft a replacement transaction T2 that double-spends T1's inputs, with high cycles but small serialized size.
3. Set T2's fee to just above `sum(T1.fee) + min_rbf_rate × T2.size / 1000`.
4. `calculate_min_replace_fee` accepts T2 because it uses size-only for `extra_rbf_fee`.
5. T2 displaces T1 while paying less than `sum(T1.fee) + min_rbf_rate × get_transaction_weight(T2.size, T2.cycles) / 1000`.

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

**File:** tx-pool/src/process.rs (L287-295)
```rust
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** util/types/src/core/tx_pool.rs (L276-303)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
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

**File:** tx-pool/src/pool.rs (L101-103)
```rust
    /// min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```
