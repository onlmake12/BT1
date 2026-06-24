Audit Report

## Title
Tx-Pool Minimum Fee Check Uses Serialized Size Instead of Full Weight, Allowing High-Cycle Transactions to Bypass Fee Rate Enforcement — (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` enforces the minimum fee rate using only the transaction's serialized byte size, while the canonical resource-cost metric (`get_transaction_weight`) is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. Because no second fee check is performed after `verify_rtx` returns the actual cycle count, an unprivileged submitter can craft a transaction with a tiny serialized size but near-maximum cycles, pay only the size-proportional minimum fee, and have the transaction admitted to the pool — consuming up to ~60× more block-cycle capacity than the fee covers. The same under-accounting recurs in `calculate_min_replace_fee` for RBF.

## Finding Description

**Root cause — size-only gate in `check_tx_fee`:**

`check_tx_fee` is called during `pre_check`, before script execution, so cycles are not yet known. The code explicitly acknowledges this:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

**No second check after cycles are known:**

In `_process_tx`, after `verify_rtx` returns `verified.cycles`, the code creates the `TxEntry` with the correct weight and immediately calls `submit_entry` — with no intervening fee-rate check using the full weight:

```rust
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
``` [2](#0-1) 

**`fee_rate()` correctly uses weight but is never enforced at admission:**

`TxEntry::fee_rate()` computes the correct weight-based fee rate:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [3](#0-2) 

where `get_transaction_weight` is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [4](#0-3) 

This value is computed but never compared against `min_fee_rate` before the entry is admitted.

**Same pattern in `calculate_min_replace_fee`:**

```rust
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
``` [5](#0-4) 

The RBF extra-fee increment is also computed from size alone, so a high-cycle replacement transaction can satisfy Rule #4 with a smaller fee increment than the weight-based threshold requires. [6](#0-5) 

## Impact Explanation

**Impact: High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

With default parameters (`min_fee_rate = 1000 shannons/KB`, `max_tx_verify_cycles = 70,000,000`, `DEFAULT_BYTES_PER_CYCLES ≈ 0.000170571`):

- A minimal transaction of ~200 bytes with 70 M cycles has a true weight of `max(200, 70_000_000 × 0.000170571) ≈ 11,940`.
- Size-based min fee: `1000 × 200 / 1000 = 200 shannons`.
- Weight-based min fee: `1000 × 11,940 / 1000 = 11,940 shannons`.

The attacker pays ~60× less than the weight-based threshold requires. Consequences:

1. **Forced expensive verification at negligible cost**: Each admitted transaction forces the node to execute up to 70 M cycles of script verification.
2. **Pool quality degradation**: Admitted entries have `fee_rate()` far below `min_fee_rate`, degrading block-template quality and displacing legitimate transactions when the pool reaches `max_tx_pool_size`.
3. **RBF weakening**: The under-counted `extra_rbf_fee` means a high-cycle replacement can satisfy Rule #4 with a smaller fee increment than intended.

## Likelihood Explanation

Any unprivileged user with RPC access to `send_transaction` can trigger this. Crafting a small transaction whose lock/type script performs near-maximum computation (e.g., a tight loop in CKB-VM stored in a dep cell, keeping the transaction itself small) is straightforward. No special privilege, key, or majority hashpower is required. The default `max_tx_verify_cycles = 70,000,000` provides a large amplification factor, and the attack is trivially repeatable. [7](#0-6) 

## Recommendation

After `verify_rtx` returns the actual `verified.cycles`, perform a second fee check using the full weight before calling `submit_entry`:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64())), snapshot));
}
```

Apply the same fix to `calculate_min_replace_fee`: replace `self.config.min_rbf_rate.fee(size as u64)` with `self.config.min_rbf_rate.fee(get_transaction_weight(size, entry_cycles))`, passing the actual cycles of the replacement entry. [8](#0-7) [9](#0-8) 

## Proof of Concept

1. Deploy a lock script that runs a tight CKB-VM loop consuming ~70 M cycles, stored in a dep cell so the transaction itself remains ~200 bytes serialized.
2. Construct a transaction spending a cell locked by that script. Set output capacity so that `fee = 200 shannons` (just above `min_fee_rate × size = 200`).
3. Submit via `send_transaction` RPC. `check_tx_fee` passes: `200 ≥ 200`.
4. The node executes 70 M cycles of script verification.
5. The admitted `TxEntry` has `fee_rate() = FeeRate::calculate(200, 11940) ≈ 16 shannons/KW`, far below the configured `min_fee_rate = 1000 shannons/KW`.
6. Repeat with many such transactions to fill the pool with entries that consume full cycle budgets but carry negligible fees, degrading pool quality and wasting node verification resources.

### Citations

**File:** tx-pool/src/util.rs (L28-53)
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
```

**File:** tx-pool/src/process.rs (L751-754)
```rust
        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
        try_or_return_with_snapshot!(ret, submit_snapshot);
```

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
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

**File:** tx-pool/src/pool.rs (L662-676)
```rust
        // Rule #4, new tx's fee need to higher than min_rbf_fee computed from the tx_pool configuration
        // Rule #3, new tx's fee need to higher than conflicts, here we only check the all conflicted txs fee
        let fee = entry.fee;
        if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
            if fee < min_replace_fee {
                return Err(Reject::RBFRejected(format!(
                    "Tx's current fee is {}, expect it to >= {} to replace old txs",
                    fee, min_replace_fee,
                )));
            }
        } else {
            return Err(Reject::RBFRejected(
                "calculate_min_replace_fee failed".to_string(),
            ));
        }
```
