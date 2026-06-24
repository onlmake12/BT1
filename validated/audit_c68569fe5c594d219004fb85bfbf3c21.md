All five code claims are verified against the actual source. Here is the validation result:

---

Audit Report

## Title
Fee Rate Unit Mismatch in `check_tx_fee` Allows High-Cycle Transactions to Bypass Minimum Fee Rate Policy - (File: tx-pool/src/util.rs)

## Summary
`check_tx_fee` computes the minimum required fee using raw serialized transaction size (`tx_size`) as the weight argument to `FeeRate::fee()`, but `FeeRate` is defined as shannons per kilo-weight where weight is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For transactions where cycles dominate, the minimum fee check is far too lenient. No second weight-based fee check is performed after `verify_rtx` determines actual cycles, so the bypass is permanent for any admitted transaction.

## Finding Description
In `tx-pool/src/util.rs` L45, `check_tx_fee` calls `tx_pool.config.min_fee_rate.fee(tx_size as u64)`, passing raw serialized size instead of actual transaction weight. The code comment at L42–44 explicitly acknowledges this as a "cheap check" using size directly, implying a more accurate check should follow — but none exists. In `_process_tx` (`tx-pool/src/process.rs` L724–753), after `verify_rtx` returns `verified.cycles` at L734, the code immediately constructs a `TxEntry` at L751 and calls `submit_entry` at L753 with no weight-based fee re-validation. The `declared_cycles` mismatch check at L736–749 only fires when `declared_cycles` is `Some(...)` — for `send_transaction` RPC callers it is `None`, so the check is skipped entirely. `FeeRate::fee(weight)` computes `rate * weight / 1000` (`fee_rate.rs` L34–36), so passing `tx_size` instead of `get_transaction_weight(tx_size, cycles)` (`tx_pool.rs` L298–303) underestimates the minimum fee by a factor of `weight / tx_size` whenever cycles dominate. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

## Impact Explanation
An unprivileged attacker can submit transactions with high cycle consumption and small serialized size, paying fees computed only against `tx_size` while forcing the node to execute scripts consuming up to `max_block_cycles` (default 70,000,000). With default `min_fee_rate = 1000` shannons/KW and a 300-byte transaction consuming 70M cycles: actual weight ≈ 11,940; correct minimum fee = 11,940 shannons; enforced minimum fee = 300 shannons — a ~40× shortfall. Repeating this floods the tx-pool with CPU-intensive transactions at a fraction of the intended economic cost, enabling sustained resource exhaustion of tx-pool CPU and memory. This matches the allowed bounty impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation
Any unprivileged caller with access to the `send_transaction` RPC can trigger this. Crafting a valid transaction with high cycle consumption requires only writing a loop-heavy RISC-V lock script — a standard capability for any CKB script author. No privileged access, leaked keys, or majority hashpower is required. The attack is repeatable and cheap to automate.

## Recommendation
After `verify_rtx` returns actual cycles in `_process_tx`, perform a second fee rate check using the true transaction weight:

```rust
// After verify_rtx returns verified.cycles (tx-pool/src/process.rs, after L734):
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee {
    return Some((Err(Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64())), snapshot));
}
```

The cheap size-only check in `check_tx_fee` can remain as an early filter, but must be followed by this weight-accurate check once actual cycles are known.

## Proof of Concept
1. Write a CKB lock script (RISC-V) that executes a tight loop consuming ~70,000,000 cycles. Compile and deploy it.
2. Craft a transaction spending a cell locked by this script. Serialized size ≈ 300 bytes.
3. Compute fee: `min_fee_rate (1000) × 300 / 1000 = 300 shannons`. Set transaction fee to exactly 300 shannons.
4. Submit via `send_transaction` RPC (sets `declared_cycles = None`, bypassing the declared cycles check).
5. `check_tx_fee` passes: `300 >= 1000 × 300 / 1000 = 300`. ✓
6. `verify_rtx` executes the script, consuming ~70,000,000 cycles.
7. No second fee check occurs. Transaction is admitted to the pool.
8. Actual weight = `max(300, 11,940)` = 11,940. Effective fee rate ≈ 25 shannons/KW — ~40× below the configured 1,000 shannons/KW minimum.
9. Repeat in a loop to flood the tx-pool with high-cycle transactions at a fraction of the intended minimum cost.

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

**File:** util/types/src/core/fee_rate.rs (L3-36)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;

impl FeeRate {
    /// Calculates the fee rate from a total fee and weight.
    pub fn calculate(fee: Capacity, weight: u64) -> Self {
        if weight == 0 {
            return FeeRate::zero();
        }
        FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
    }

    /// Creates a fee rate from shannons per kilo-weight.
    pub const fn from_u64(fee_per_kw: u64) -> Self {
        FeeRate(fee_per_kw)
    }

    /// Returns the fee rate as shannons per kilo-weight.
    pub const fn as_u64(self) -> u64 {
        self.0
    }

    /// Creates a zero fee rate.
    pub const fn zero() -> Self {
        Self::from_u64(0)
    }

    /// Calculates the fee for a given weight.
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
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
