### Title
Tx-Pool Min-Fee-Rate Admission Check Uses Only Serialized Size, Not Actual Weight — Cycle-Heavy Transactions Bypass the Minimum Fee Rate Guard - (File: `tx-pool/src/util.rs`)

---

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size. However, the actual block weight used for mining selection and pool eviction is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. A transaction sender can craft a small-serialized-size but high-cycle transaction that passes the admission gate while its true effective fee rate is far below `min_fee_rate`. This is the direct CKB analog of the `DynamicAfterFee` bug where fee logic only applied to one transaction type (exact-input), allowing the other type (exact-output) to bypass it entirely.

---

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` computes the minimum required fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(reject);
}
``` [1](#0-0) 

The actual transaction weight used everywhere else in the system — for pool sorting, eviction, and block assembly — is defined as:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. The `TxEntry::fee_rate()` method uses this correct weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [3](#0-2) 

The admission check in `check_tx_fee` is called during `pre_check` before script execution, so `cycles` are not yet known at that point. The fee check uses only `tx_size`: [4](#0-3) 

After verification, the actual `cycles` are stored in the `TxEntry`, but no second fee-rate check against the real weight is performed: [5](#0-4) 

---

### Impact Explanation

An unprivileged RPC caller submitting via `send_transaction` can craft a transaction with:

- **Small serialized size** (e.g., 300 bytes) — passes `check_tx_fee` with fee ≥ `min_fee_rate * 300 / 1000 = 300 shannons`
- **High cycle consumption** (e.g., 70,000,000 cycles, the `max_tx_verify_cycles` default)
- **Actual weight** = `max(300, 70_000_000 × 0.000_170_571_4)` ≈ `11,940`
- **Actual effective fee rate** = `300 × 1000 / 11,940` ≈ **25 shannons/KW** — far below the configured `min_fee_rate` of 1,000 shannons/KW

Such a transaction is admitted to the pool despite having an effective fee rate ~40× below the minimum. The `min_fee_rate` economic protection — which exists to prevent pool spam and ensure miners are compensated per unit of block resource consumed — is entirely bypassed for the cycle-dominant dimension of block weight. [6](#0-5) 

---

### Likelihood Explanation

- **Entry path**: Any unprivileged user with RPC access to `send_transaction` (the standard public interface) can submit such a transaction.
- **Ease**: Crafting a small-serialized transaction with a script that consumes many cycles is straightforward — a tight loop in a lock script suffices.
- **No special privileges required**: No key, no operator access, no majority hashpower.
- **Confirmed by code comment**: The code itself acknowledges the discrepancy ("Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check"), confirming this is a known approximation with real consequences. [7](#0-6) 

---

### Recommendation

After script verification completes and `verified.cycles` is known, perform a second fee-rate check using the actual weight:

```rust
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
// Re-check fee rate against actual weight (size + cycles)
let actual_fee_rate = entry.fee_rate();
if actual_fee_rate < tx_pool.config.min_fee_rate {
    return Err(Reject::LowFeeRate(...));
}
```

This mirrors the pattern used in `TxEntry::fee_rate()` and `get_transaction_weight`, ensuring the admission gate is consistent with the weight model used for block packing. [8](#0-7) 

---

### Proof of Concept

1. Construct a CKB transaction with:
   - A lock script that runs a tight computation loop consuming ~70,000,000 cycles
   - Minimal witnesses and outputs so serialized size is ~300 bytes
   - Fee = 300 shannons (= `min_fee_rate * tx_size / 1000` = `1000 * 300 / 1000`)

2. Submit via `send_transaction` RPC.

3. `check_tx_fee` computes `min_fee = 1000 * 300 / 1000 = 300 shannons`. Fee (300) ≥ min_fee (300) → **admitted**.

4. After verification, `verified.cycles ≈ 70_000_000`. Actual weight = `max(300, 70_000_000 × 0.000_170_571_4)` ≈ 11,940. Actual fee rate = `300 × 1000 / 11,940 ≈ 25 shannons/KW`.

5. The transaction is in the pool with an effective fee rate of 25 shannons/KW — 40× below the configured `min_fee_rate` of 1,000 shannons/KW — with no rejection. [9](#0-8) [10](#0-9)

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/process.rs (L288-290)
```rust
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
```

**File:** tx-pool/src/process.rs (L751-753)
```rust
        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
```
