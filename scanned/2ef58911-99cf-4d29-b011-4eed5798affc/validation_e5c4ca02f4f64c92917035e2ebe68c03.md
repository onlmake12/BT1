### Title
`check_tx_fee` Uses Serialized Size Instead of Transaction Weight for Minimum Fee Enforcement, Allowing High-Cycle Transactions to Bypass Effective Minimum Fee Rate - (`tx-pool/src/util.rs`)

### Summary

The tx-pool's pre-admission fee check computes the minimum required fee using only the transaction's serialized byte size, ignoring the actual CKB-VM cycle consumption. Because CKB's true transaction weight is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`, a transaction with a small serialized size but near-maximum cycle consumption can pass the minimum fee check while having an actual weight-based fee rate far below the configured `min_fee_rate`. No second fee check is performed after script execution reveals the true cycle count. This allows any unprivileged transaction submitter (RPC caller or P2P relay peer) to admit transactions to the pool at a fraction of the intended economic cost, consuming node verification resources and occupying pool space with transactions miners will never include.

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` computes the minimum required fee as:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The actual transaction weight used everywhere else in the system (mining selection, pool eviction, fee estimation) is computed by `get_transaction_weight`:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. For a transaction with `max_tx_verify_cycles = 70,000,000` cycles and a serialized size of ~200 bytes, the true weight is `max(200, 70_000_000 × 0.000_170_571_4) ≈ 11,940`. The size-based minimum fee at 1,000 shannons/KW is only 200 shannons, while the weight-based minimum should be 11,940 shannons — a ~60× discrepancy.

`check_tx_fee` is called in `pre_check` before script execution:

```rust
let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
``` [3](#0-2) 

After `verify_rtx` completes and the actual cycle count is known, no second fee check is performed against the weight-based minimum. The transaction is admitted to the pool with its actual (very low) effective fee rate.

The `FeeRate::fee` function confirms the size-only path:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;
    Capacity::shannons(fee)
}
``` [4](#0-3) 

The default `min_fee_rate` is 1,000 shannons/KW and `max_tx_verify_cycles` is 70,000,000: [5](#0-4) 

### Impact Explanation

An attacker submitting a transaction with ~200 bytes serialized size, 70M cycles of script execution, and exactly 200 shannons fee:

1. Passes `check_tx_fee` (200 ≥ 1,000 × 200 / 1,000 = 200 shannons).
2. Forces the node to execute 70M cycles of CKB-VM computation during `verify_rtx`.
3. Is admitted to the pool with an actual fee rate of ≈16 shannons/KW — 60× below the configured minimum.
4. Is never selected by miners (lowest priority by weight-based fee rate).
5. Occupies pool space for up to 12 hours (default `expiry_hours`) or until the pool fills and eviction occurs.

By repeating this, an attacker can continuously consume node verification resources and fill the mempool with economically unviable "dust" transactions, degrading the fee market signal and node performance. The pool eviction mechanism (`max_tx_pool_size = 180MB`) provides a backstop but does not prevent the verification-cost DoS. [6](#0-5) 

### Likelihood Explanation

Any unprivileged RPC caller (`send_transaction`) or P2P relay peer can submit such a transaction. Crafting a transaction with high cycle consumption but small serialized size is straightforward — a script that runs a tight computation loop in a small binary achieves this. The attacker's cost is only the size-based minimum fee (a few hundred shannons per transaction), making sustained attacks economically viable.

### Recommendation

After `verify_rtx` returns the actual cycle count, perform a second fee check using the true transaction weight:

```rust
let weight = get_transaction_weight(tx_size, completed.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(
        tx_pool.config.min_fee_rate,
        min_fee_by_weight.as_u64(),
        fee.as_u64(),
    ));
}
```

This ensures that high-cycle transactions pay fees commensurate with their actual resource consumption before being admitted to the pool.

### Proof of Concept

1. Craft a CKB transaction whose lock or type script executes a tight loop consuming ~70,000,000 cycles (the `max_tx_verify_cycles` default). The script binary itself can be small (a few hundred bytes), keeping the serialized transaction size at ~200 bytes.
2. Set the transaction fee to exactly `ceil(min_fee_rate × tx_size / 1000)` = 200 shannons.
3. Submit via `send_transaction` RPC.
4. Observe: the transaction passes `check_tx_fee` (size-based check passes), the node executes 70M cycles during verification, and the transaction is admitted to the pool.
5. Query `get_transaction` — the transaction is in the pool with status `pending`.
6. Observe that miners never include it (effective fee rate ≈16 shannons/KW, far below the 1,000 shannons/KW minimum).
7. Repeat to continuously consume node verification resources and pool space at ~1/60th of the intended economic cost.

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
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

**File:** tx-pool/src/process.rs (L289-290)
```rust
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L10-14)
```rust
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```

**File:** util/app-config/src/legacy/tx_pool.rs (L18-20)
```rust
const DEFAULT_EXPIRY_HOURS: u8 = 12;
// Default max_tx_pool_size 180mb
const DEFAULT_MAX_TX_POOL_SIZE: usize = 180_000_000;
```
