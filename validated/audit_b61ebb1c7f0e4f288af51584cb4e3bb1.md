Audit Report

## Title
Minimum Fee Rate Admission Check Uses Serialized Size Instead of Transaction Weight, Allowing High-Cycles Transactions to Bypass Effective Fee Rate Enforcement - (`tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size, while the effective fee rate used for pool scoring and eviction is computed via `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A transaction with near-maximum cycles (~70M) but small serialized size (~200 bytes) passes admission paying only ~200 shannons, yet its weight-based effective fee rate is ~16.7 shannons/KB — roughly 60× below the configured 1000 shannons/KB minimum. The code comment in `check_tx_fee` explicitly acknowledges this discrepancy but no secondary weight-based check exists in the admission path.

## Finding Description

`check_tx_fee` is called during `pre_check` (before script verification, when cycles are not yet known) and uses only `tx_size` for the minimum fee calculation:

```rust
// tx-pool/src/util.rs L42-45
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The call sites in `pre_check` confirm cycles are unavailable at this point — `check_tx_fee` receives only `tx_size`, not cycles: [2](#0-1) 

After admission, `TxEntry::fee_rate()` and `EvictKey` both use `get_transaction_weight`: [3](#0-2) [4](#0-3) 

`get_transaction_weight` is defined as: [5](#0-4) 

With `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` and `max_tx_verify_cycles = 70_000_000`, the maximum cycles-equivalent weight is ~11,940 bytes. For a 200-byte transaction at 70M cycles:

- **Admission check**: `min_fee = 1000 × 200 / 1000 = 200 shannons` → passes
- **Actual weight**: `max(200, 11940) = 11940`
- **Effective fee rate**: `200 / 11940 × 1000 ≈ 16.7 shannons/KB` — 60× below minimum

The eviction mechanism (`EvictKey`) does use weight-based fee rate, so these transactions are evicted first when the pool is full. However, this does not prevent admission — the attacker can continuously resubmit, causing persistent pool churn and wasted script verification resources (up to 70M VM cycles per submitted transaction). [6](#0-5) 

## Impact Explanation

This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. An attacker can submit transactions at 60× lower effective cost than the fee rate was designed to enforce. Each submission forces the node to execute up to 70M VM cycles of script verification. While the pool evicts these transactions first when full, the attacker can continuously resubmit, causing sustained verification CPU exhaustion and pool churn. Legitimate transactions are repeatedly displaced and re-admitted, degrading pool quality and fee market signals across the network.

## Likelihood Explanation

Any unprivileged user with valid UTXOs can trigger this. Crafting a transaction with a loop-heavy lock or type script consuming ~70M cycles while keeping serialized size small (~200 bytes) is straightforward. Submission is possible via the `send_transaction` RPC or P2P relay protocol. The attacker pays only the size-based minimum fee (e.g., 200 shannons per transaction), which is 60× cheaper than the weight-based minimum the fee rate was designed to enforce. No special privileges, leaked keys, or victim mistakes are required.

## Recommendation

Replace the size-only check in `check_tx_fee` with a weight-based check after script verification (when cycles are known). Since `check_tx_fee` is called pre-verification, the fix requires either:

1. Deferring the weight-based fee check to after `verify_rtx` returns the actual cycles, then constructing `TxEntry` and checking `entry.fee_rate() >= min_fee_rate`, or
2. Using a conservative upper-bound estimate of cycles at pre-check time (e.g., `max_tx_verify_cycles`) to compute a worst-case weight for the admission check.

The weight-based check should mirror `TxEntry::fee_rate()`:
```rust
let weight = get_transaction_weight(tx_size, cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
``` [7](#0-6) 

## Proof of Concept

1. Deploy a CKB lock script that executes a tight computation loop consuming ~70,000,000 cycles. Keep the serialized transaction size small (~200 bytes by minimizing inputs, outputs, and witness data).
2. Set the transaction fee to exactly `min_fee_rate × tx_size / 1000 = 1000 × 200 / 1000 = 200 shannons`.
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 200 shannons`; fee = 200 shannons ≥ min_fee → **admitted**.
5. `TxEntry::fee_rate()` computes weight = max(200, 11940) = 11940; effective fee rate ≈ 16.7 shannons/KB — 60× below minimum.
6. The node executes 70M VM cycles to verify the script.
7. Repeat continuously with new UTXOs. Each submission forces 70M cycles of verification at 60× below the intended minimum cost, causing sustained CPU load and pool churn across all nodes that relay the transaction.

### Citations

**File:** tx-pool/src/util.rs (L28-54)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
    Ok(fee)
}
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

**File:** util/types/src/core/tx_pool.rs (L276-279)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;
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
