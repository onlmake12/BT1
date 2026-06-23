### Title
Fee Rate Enforcement Uses Transaction Size Instead of Weight, Allowing High-Cycle Transactions to Bypass `min_fee_rate` - (File: tx-pool/src/util.rs)

---

### Summary

In `check_tx_fee` (`tx-pool/src/util.rs`), the minimum required fee is computed by calling `min_fee_rate.fee(tx_size)`, which treats the raw serialized byte count (`tx_size`) as the weight parameter. However, `FeeRate` is defined as **shannons per kilo-weight**, where weight = `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For transactions where script cycles dominate, the actual weight can be up to ~60× larger than `tx_size`. Because this is the **only** fee-rate check in the tx-pool admission path, an unprivileged RPC caller can submit transactions whose effective fee rate is far below the configured `min_fee_rate`, bypassing the intended spam-prevention floor.

---

### Finding Description

**Root cause — unit mismatch in `check_tx_fee`:**

`FeeRate` is documented as "shannons per kilo-weight": [1](#0-0) 

Weight is defined as: [2](#0-1) 

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`, so with `max_tx_verify_cycles = 70_000_000`, the maximum weight from cycles alone is `70_000_000 × 0.000_170_571_4 ≈ 11,940` bytes.

But `check_tx_fee` computes the minimum fee using `tx_size` (raw serialized bytes) as the weight: [3](#0-2) 

The code comment explicitly acknowledges the theoretical incorrectness ("Theoretically we cannot use size as weight directly to calculate fee_rate"), yet this is the **only** fee-rate check in the admission path. After `verify_rtx` determines the actual cycles, no second fee-rate check using the real weight is performed. [4](#0-3) 

**Concrete discrepancy:**

| Parameter | Value |
|---|---|
| `tx_size` | 200 bytes |
| `min_fee_rate` | 1,000 shannons/KW |
| `min_fee` (check uses) | `1000 × 200 / 1000 = 200 shannons` |
| Actual weight (max cycles) | `max(200, 11940) = 11,940 bytes` |
| Effective fee rate | `200 × 1000 / 11940 ≈ 16.7 shannons/KW` |

The effective enforcement is ~60× weaker than the configured `min_fee_rate` for maximum-cycle transactions.

---

### Impact Explanation

An unprivileged RPC caller can submit transactions with high script cycles (up to `max_tx_verify_cycles = 70,000,000`) and small serialized size, paying a fee just above `min_fee_rate × tx_size / 1000`. These transactions enter the tx-pool with an effective fee rate ~60× below the configured floor. This enables:

1. **Tx-pool spam**: Filling the 180 MB pool with computationally expensive, low-fee-rate transactions at negligible cost (e.g., 200 shannons per transaction).
2. **Displacement of legitimate transactions**: Low-fee-rate transactions occupy pool space, potentially evicting or delaying higher-priority legitimate transactions.
3. **Miner revenue distortion**: Block assemblers sort by fee rate; artificially low-fee-rate transactions pollute the ordering.

The attack does not cause direct fund loss but degrades node availability and tx-pool integrity.

---

### Likelihood Explanation

- **Entry path**: Any caller of `send_transaction` or `send_test_transaction` RPC — no privilege required.
- **Feasibility**: Crafting a transaction with high cycles (e.g., a script containing a computation loop) and minimal inputs/outputs (small size) is straightforward for any script author.
- **Cost**: Negligible — 200 shannons per transaction at the example parameters.
- **Persistence**: Transactions expire after `expiry_hours` (default 12 hours), so the attacker must continuously resubmit, but the cost remains trivial.

---

### Recommendation

After `verify_rtx` determines the actual cycles, perform a second fee-rate check using the real weight:

```rust
// After verify_rtx returns actual cycles:
let actual_weight = get_transaction_weight(tx_size, actual_cycles);
let min_fee_accurate = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee_accurate {
    return Err(Reject::LowFeeRate(...));
}
```

Alternatively, enforce the fee-rate check using the declared cycles (provided by the submitter) before script execution, accepting that declared cycles may be an overestimate but never an underestimate of actual resource consumption.

---

### Proof of Concept

1. Craft a CKB transaction with:
   - A lock/type script that executes ~70,000,000 cycles (e.g., a tight loop in a RISC-V binary).
   - Minimal inputs and outputs so `tx_size ≈ 200` bytes.
   - Fee = `min_fee_rate × tx_size / 1000 + 1` = 201 shannons (with default `min_fee_rate = 1000`).

2. Submit via `send_transaction` RPC.

3. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200 shannons`. Fee (201) ≥ min_fee (200) → **passes**.

4. `verify_rtx` runs the script, consuming ~70,000,000 cycles. Actual weight = 11,940 bytes. Actual fee rate = `201 × 1000 / 11940 ≈ 16.8 shannons/KW` — far below the 1,000 shannons/KW floor.

5. Transaction enters the pool. Repeat to fill the 180 MB pool with ~60× underpriced transactions.

**Relevant code locations:** [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** util/types/src/core/fee_rate.rs (L3-5)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);
```

**File:** util/types/src/core/fee_rate.rs (L11-16)
```rust
    pub fn calculate(fee: Capacity, weight: u64) -> Self {
        if weight == 0 {
            return FeeRate::zero();
        }
        FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
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

**File:** tx-pool/src/process.rs (L289-290)
```rust
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
```
