Audit Report

## Title
Minimum Fee Rate Admission Check Uses Serialized Size Instead of Transaction Weight, Allowing High-Cycles Transactions to Bypass Effective Fee Rate Enforcement - (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` enforces the minimum fee rate using only the transaction's serialized byte size, while the effective fee rate used for pool scoring and eviction uses `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A transaction with near-maximum cycles (~70M) but small serialized size (~200 bytes) passes admission paying only ~200 shannons, yet its weight-based effective fee rate is ~16.7 shannons/KB — roughly 60× below the configured 1000 shannons/KB minimum. No secondary weight-based check exists anywhere in the post-verification admission path.

## Finding Description

`check_tx_fee` in `tx-pool/src/util.rs` L42–45 uses only `tx_size` for the minimum fee calculation:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

This function is called in `pre_check` at `process.rs` L289 and L294, before `verify_rtx` is invoked at L724 — meaning cycles are structurally unavailable at the point of the fee check. [2](#0-1) 

After `verify_rtx` returns the actual cycles at L734, the code in `_process_tx` only checks for `DeclaredWrongCycles` (L736–748) and then proceeds to pool admission. There is no secondary weight-based fee check after cycles are known. [3](#0-2) 

Meanwhile, `TxEntry::fee_rate()` and `EvictKey` both use `get_transaction_weight`: [4](#0-3) [5](#0-4) 

`get_transaction_weight` is defined as `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)` where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`: [6](#0-5) 

**Arithmetic:**
- At 70M cycles: `70,000,000 × 0.000_170_571_4 ≈ 11,940` bytes weight
- For a 200-byte tx: `weight = max(200, 11940) = 11940`
- Admission check: `min_fee = 1000 × 200 / 1000 = 200 shannons` → **passes**
- Effective fee rate after admission: `200 / 11940 × 1000 ≈ 16.7 shannons/KB` — 60× below minimum

The code comment at L42–44 explicitly acknowledges the discrepancy but treats it as acceptable for a "cheap check." No compensating check exists post-verification.

## Impact Explanation

This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. An attacker pays only the size-based minimum fee (e.g., 200 shannons) — 60× cheaper than the weight-based minimum the fee rate was designed to enforce — while forcing the node to execute up to 70M VM cycles of script verification per submission. The eviction mechanism does use weight-based fee rate, so these transactions are evicted first when the pool is full, but this does not prevent admission. The attacker can continuously resubmit with new UTXOs, causing sustained verification CPU exhaustion and pool churn. Legitimate transactions are repeatedly displaced, degrading pool quality and fee market signals across all relaying nodes.

## Likelihood Explanation

Any unprivileged user with valid UTXOs can trigger this. Crafting a transaction with a loop-heavy lock or type script consuming ~70M cycles while keeping serialized size small (~200 bytes) is straightforward. Submission is possible via the `send_transaction` RPC or P2P relay protocol. No special privileges, leaked keys, or victim mistakes are required. The attack is repeatable at minimal cost.

## Recommendation

After `verify_rtx` returns the actual cycles in `_process_tx`, add a weight-based fee check before pool admission:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

This mirrors `TxEntry::fee_rate()` and closes the gap between the admission check and the effective fee rate used for scoring and eviction. The existing size-only pre-check in `check_tx_fee` can remain as a cheap early filter, but must be supplemented by this post-verification weight-based check.

## Proof of Concept

1. Deploy a CKB lock script that executes a tight computation loop consuming ~70,000,000 cycles. Keep the serialized transaction size small (~200 bytes by minimizing inputs, outputs, and witness data).
2. Set the transaction fee to exactly `min_fee_rate × tx_size / 1000 = 1000 × 200 / 1000 = 200 shannons`.
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 200 shannons`; fee = 200 shannons ≥ min_fee → **admitted**.
5. `verify_rtx` executes 70M VM cycles to verify the script.
6. No post-verification weight check exists; the transaction enters the pool.
7. `TxEntry::fee_rate()` computes weight = max(200, 11940) = 11940; effective fee rate ≈ 16.7 shannons/KB — 60× below minimum.
8. Repeat continuously with new UTXOs. Each submission forces 70M cycles of verification at 60× below the intended minimum cost, causing sustained CPU load and pool churn across all nodes that relay the transaction.

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
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

**File:** tx-pool/src/process.rs (L724-750)
```rust
        let verified_ret = verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            command_rx,
        )
        .await;

        let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

        if let Some(declared) = declared_cycles
            && declared != verified.cycles
        {
            info!(
                "process_tx declared cycles not match verified cycles, declared: {}, verified: {}, tx_hash: {}",
                declared,
                verified.cycles,
                tx.hash()
            );
            return Some((
                Err(Reject::DeclaredWrongCycles(declared, verified.cycles)),
                snapshot,
            ));
        }

```

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/component/entry.rs (L234-247)
```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);

        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            timestamp: entry.timestamp,
            descendants_count: entry.descendants_count,
        }
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
