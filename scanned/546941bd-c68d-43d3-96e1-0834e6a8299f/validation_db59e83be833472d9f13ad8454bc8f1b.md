### Title
Tx-Pool Minimum Fee Check Uses Serialized Size Instead of Full Weight, Allowing High-Cycle Transactions to Bypass Fee Rate Enforcement — (File: `tx-pool/src/util.rs`)

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size, while the canonical resource-cost metric (`get_transaction_weight`) is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. An unprivileged tx-pool submitter can craft a transaction with a tiny serialized size but near-maximum cycles, pay only the size-proportional minimum fee, and have the transaction admitted to the pool — consuming up to ~60× more block-cycle capacity than the fee covers. The same under-accounting recurs in `calculate_min_replace_fee` for RBF.

### Finding Description

`check_tx_fee` is called during `pre_check`, before script execution, so cycles are not yet known. The code acknowledges this explicitly:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

After verification, the actual cycles are known and the `TxEntry` is created with the correct weight:

```rust
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
``` [2](#0-1) 

But there is **no second fee check** after cycles are determined. The entry is submitted directly to the pool with whatever fee passed the size-only gate. The `fee_rate()` method on `TxEntry` correctly uses the full weight:

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

The same pattern appears in `calculate_min_replace_fee`, which computes the RBF extra fee using only `size`:

```rust
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
``` [5](#0-4) 

### Impact Explanation

With default parameters (`min_fee_rate = 1000 shannons/KB`, `max_tx_verify_cycles = 70,000,000`, `DEFAULT_BYTES_PER_CYCLES ≈ 0.000170571`):

- A minimal transaction of ~200 bytes with 70 M cycles has a true weight of `max(200, 70_000_000 × 0.000170571) ≈ 11,940`.
- Size-based min fee: `1000 × 200 / 1000 = 200 shannons`.
- Weight-based min fee: `1000 × 11,940 / 1000 = 11,940 shannons`.

The attacker pays **~60× less** than the weight-based threshold requires, yet the transaction occupies the full cycle budget of a block. Submitting many such transactions:

1. Forces the node to execute up to 70 M cycles of script verification per transaction at negligible fee cost.
2. Fills the pool with entries whose actual `fee_rate()` is far below `min_fee_rate`, degrading block-template quality.
3. Displaces legitimate transactions when the pool reaches `max_tx_pool_size` (180 MB default), since eviction is weight-based but admission was size-based.

For RBF, the under-counted `extra_rbf_fee` means a replacement transaction with high cycles can satisfy Rule #4 with a smaller fee increment than intended, weakening the RBF anti-spam guarantee. [6](#0-5) 

### Likelihood Explanation

Any unprivileged user with RPC access to `send_transaction` can trigger this. Crafting a small transaction whose lock/type script performs near-maximum computation (e.g., a tight loop in CKB-VM) is straightforward. No special privilege, key, or majority hashpower is required. The default `max_tx_verify_cycles = 70,000,000` provides a large amplification factor. [7](#0-6) 

### Recommendation

After `verify_rtx` returns the actual `verified.cycles`, perform a second fee check using the full weight before calling `submit_entry`:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

Apply the same fix to `calculate_min_replace_fee`: replace `self.config.min_rbf_rate.fee(size as u64)` with `self.config.min_rbf_rate.fee(get_transaction_weight(size, entry_cycles))`. [8](#0-7) 

### Proof of Concept

1. Deploy a lock script that runs a tight CKB-VM loop consuming ~70 M cycles but whose serialized code is ≤ 200 bytes (e.g., stored as a dep cell; the transaction itself references it with a small witness).
2. Construct a transaction spending a cell locked by that script. Serialized transaction size ≈ 200 bytes. Set output capacity so that `fee = 200 shannons` (just above `min_fee_rate × size = 200`).
3. Submit via `send_transaction` RPC. The `check_tx_fee` gate passes (200 ≥ 200).
4. The node executes 70 M cycles of script verification.
5. The admitted `TxEntry` has `fee_rate() = FeeRate::calculate(200, 11940) ≈ 16 shannons/KW`, far below the configured `min_fee_rate = 1000 shannons/KW`.
6. Repeat with many such transactions to fill the pool with entries that consume full cycle budgets but carry negligible fees, degrading pool quality and wasting node verification resources.

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

**File:** tx-pool/src/process.rs (L734-753)
```rust
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

        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
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

**File:** tx-pool/src/pool.rs (L103-103)
```rust
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

**File:** util/app-config/src/configs/tx_pool.rs (L21-21)
```rust
    pub max_tx_verify_cycles: Cycle,
```
