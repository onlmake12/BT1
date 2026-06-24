Audit Report

## Title
Tx-Pool Admission Check Uses Serialized Size Only While Actual Fee Rate Uses Full Weight (Cycles-Inclusive), Allowing High-Cycle Transactions to Bypass Minimum Fee Rate Enforcement — (File: tx-pool/src/util.rs)

## Summary
`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using only the transaction's serialized byte size, while `TxEntry::fee_rate()` uses the full transaction weight (`max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`). Because no post-verification fee check using the full weight exists, an attacker can craft a small-serialized, high-cycle transaction that passes the admission gate with a trivial fee, then sits in the pool with an effective fee rate up to ~60× below the operator-configured minimum — after forcing the node to run full CKB-VM script verification.

## Finding Description

**Path 1 — Admission gate (size only):**

`check_tx_fee` is called in `pre_check` (process.rs L289, L294) before script verification. It computes:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The inline comment explicitly acknowledges the incompleteness: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly."* No second fee-rate check using cycles is performed anywhere after `verify_rtx` completes and actual cycles are known.

**Path 2 — Pool entry fee rate (full weight):**

Once admitted, every `TxEntry` computes its actual fee rate as:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [2](#0-1) 

`get_transaction_weight` is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`: [3](#0-2) 

With `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` and `max_tx_verify_cycles = 70_000_000`, the maximum cycle-derived weight is `70_000_000 × 0.000_170_571_4 ≈ 11,940 bytes`.

**The same inconsistency exists in RBF extra-fee calculation:**

```rust
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
``` [4](#0-3) 

**Concrete divergence for a 200-byte, 70M-cycle transaction at `min_fee_rate = 1000 shan/KB`:**

| Metric | Admission check | Pool entry `fee_rate()` |
|---|---|---|
| Denominator | 200 bytes | `max(200, 11940) = 11940` |
| Min fee required | 200 shannons | 11,940 shannons |
| Fee paid (201 shan) | passes ✓ | ≈ 16.8 shan/KB (~60× below minimum) |

The flow in `pre_check` calls `check_tx_fee` before verification: [5](#0-4) 

After `verify_rtx` returns with actual cycles, there is no subsequent call to re-validate the fee rate against the full weight.

## Impact Explanation

This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

An attacker can flood the tx-pool with transactions whose effective fee rate is up to ~60× below the operator-configured `min_fee_rate`. Each such transaction forces the node to execute full CKB-VM script verification (up to 70M cycles), consuming significant CPU before the transaction is eventually evicted by `limit_size`. The eviction mechanism uses `fee_rate()` (full weight), so these transactions are eventually removed — but only after verification CPU has been spent and pool capacity consumed. Repeating the attack continuously exhausts the verify queue and degrades node performance for legitimate transactions.

## Likelihood Explanation

The attack requires no special privileges. Any RPC caller or P2P relay peer can execute it:

1. Deploy a CKB-VM RISC-V script that loops to consume ~70M cycles (trivial).
2. Lock a cell with that script.
3. Craft a transaction with small serialized size (~200 bytes) and fee just above `min_fee_rate × size / 1000`.
4. Submit via `send_transaction` RPC or P2P relay.

The attack is repeatable, cheap (minimal fee per transaction), and requires no majority hashpower or social engineering. The maximum weight divergence is bounded by `max_tx_verify_cycles` (~60× at default settings), but this is sufficient to sustain a continuous CPU-exhaustion and pool-congestion attack.

## Recommendation

After script verification completes and actual cycles are known, perform a second fee-rate check using the full weight before inserting the entry into the pool:

```rust
let weight = get_transaction_weight(tx_size, completed.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64()));
}
```

Apply the same fix to `calculate_min_replace_fee` for RBF, using the full weight of the replacement transaction instead of size only. This mirrors the pattern already used in eviction, scoring, and `fee_rate()` throughout the pool.

## Proof of Concept

```
1. Deploy a CKB-VM RISC-V script that loops until it consumes ~70M cycles.
2. Lock a cell with that script on-chain.
3. Build a transaction spending that cell:
      serialized_size ≈ 200 bytes
      cycles          ≈ 70,000,000
      fee             = 201 shannons  (just above min_fee_rate × 200 / 1000 = 200 at 1000 shan/KB)
4. Submit via RPC: ckb_sendTransaction(tx)
5. check_tx_fee passes: 201 ≥ 200  ✓  (size-only check)
6. verify_rtx runs full CKB-VM execution: ~70M cycles of CPU consumed
7. TxEntry is inserted; TxEntry::fee_rate() = 201 × 1000 / 11940 ≈ 16.8 shan/KB
      — ~60× below the configured minimum of 1000 shan/KB
8. Transaction occupies pool space until evicted by limit_size
9. Repeat continuously to saturate the verify queue and pool capacity,
   degrading throughput for legitimate transactions.
```

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
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

**File:** tx-pool/src/process.rs (L286-295)
```rust
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
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
