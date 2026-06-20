### Title
Minimum Fee Rate Enforced Against Serialized Size Only, Not Actual Transaction Weight — (`tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size (`tx_size`) as the weight denominator. The actual transaction weight — used everywhere else in the system for prioritization, eviction, and block assembly — is `get_transaction_weight(tx_size, cycles) = max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. A transaction with a small serialized size but high cycle consumption can pass the admission fee check while having an effective fee rate far below `min_fee_rate`, directly analogous to Paladin charging fees against the theoretical maximum pledge rather than actual utilization.

---

### Finding Description

The admission gate in `check_tx_fee` computes the minimum required fee as:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The actual weight formula used for block assembly, fee-rate sorting, and eviction is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

with `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [3](#0-2) 

The admission check uses only `tx_size` (the lower bound of the actual weight), while every downstream consumer — `TxEntry::fee_rate()`, `AncestorsScoreSortKey`, `EvictKey`, and the fee-rate estimator — uses the full `get_transaction_weight(size, cycles)`. [4](#0-3) 

The same size-only denominator is used in `calculate_min_replace_fee` for RBF:

```rust
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
``` [5](#0-4) 

---

### Impact Explanation

An attacker submits a transaction with:
- Small serialized size (e.g., 200 bytes)
- A lock script that consumes cycles up to `max_tx_verify_cycles` (default 70,000,000 per `ckb.toml`) [6](#0-5) 

Concrete arithmetic with default config (`min_fee_rate = 1,000` shannons/KB):

| Parameter | Value |
|---|---|
| `tx_size` | 200 bytes |
| `cycles` | 70,000,000 |
| Cycle-based weight | 70,000,000 × 0.000_170_571_4 ≈ **11,940 bytes** |
| Actual weight | max(200, 11,940) = **11,940 bytes** |
| Fee check minimum | 1,000 × 200 / 1,000 = **200 shannons** |
| Effective fee rate | 200 / 11.94 ≈ **16.7 shannons/KB** |
| Ratio to `min_fee_rate` | **~1.67%** |

The transaction passes admission at ~1.67% of the intended minimum fee rate — a ~60× bypass. An attacker can flood the tx-pool with such transactions at a fraction of the intended anti-spam cost, consuming both the pool's size budget and its cycle budget, and displacing legitimate transactions.

---

### Likelihood Explanation

The attack requires only an unprivileged `send_transaction` RPC call or a P2P relay submission. No special keys, majority hashpower, or privileged access are needed. The attacker needs only to compile a RISC-V script that loops for many cycles (trivially achievable) and keep the transaction's serialized body small. The discrepancy is structural and present in every node running the default configuration.

---

### Recommendation

Replace the size-only denominator in `check_tx_fee` with the actual transaction weight. Since cycles are known at the point `check_tx_fee` is called (after `verify_rtx` returns the `Completed` entry with cycle count), pass cycles into the check:

```rust
// Instead of:
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);

// Use:
let weight = get_transaction_weight(tx_size, cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

Apply the same fix to `calculate_min_replace_fee`, replacing `size` with `get_transaction_weight(size, cycles)` for the `extra_rbf_fee` calculation. [7](#0-6) 

---

### Proof of Concept

1. Compile a minimal RISC-V lock script that executes a tight loop consuming ~70,000,000 cycles. The binary can be under 200 bytes.
2. Create a transaction spending any live cell locked by this script. Serialized `tx_size` ≈ 200 bytes.
3. Set the transaction fee to exactly `min_fee_rate * 200 / 1000 = 200 shannons` (with `min_fee_rate = 1,000`).
4. Submit via `send_transaction` RPC.
5. **Expected (correct) behavior**: rejected with `LowFeeRate` because actual weight ≈ 11,940 bytes requires ≥ 11,940 shannons.
6. **Actual behavior**: accepted, because `check_tx_fee` only checks `fee >= min_fee_rate * tx_size / 1000 = 200 shannons`.
7. Repeat with many such transactions to fill the pool at ~1.67% of the intended minimum cost per unit of actual block weight consumed. [8](#0-7) [9](#0-8)

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

**File:** tx-pool/src/pool.rs (L101-103)
```rust
    /// min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```

**File:** resource/ckb.toml (L215-215)
```text
max_tx_verify_cycles = 70_000_000
```
