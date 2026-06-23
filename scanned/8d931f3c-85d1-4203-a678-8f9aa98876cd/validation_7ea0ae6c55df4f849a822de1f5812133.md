### Title
`tx_size` (bytes) Used as `weight` in Fee-Rate Admission and RBF Checks, Allowing Cycle-Heavy Transactions to Bypass Minimum Fee Rate — (`tx-pool/src/util.rs`, `tx-pool/src/pool.rs`)

---

### Summary

`check_tx_fee()` and `calculate_min_replace_fee()` both pass raw serialized byte size (`tx_size`) to `FeeRate::fee()`, which expects a **weight** value (`max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`). For cycle-heavy transactions, weight can be up to ~60× larger than size. This means the minimum-fee-rate gate and the RBF minimum-increment gate are both computed against the wrong unit, allowing an unprivileged submitter to admit transactions whose true fee rate is far below the configured threshold.

---

### Finding Description

`FeeRate` is defined as **shannons per kilo-weight**, where weight is:

```
weight = max(tx_size_bytes, cycles × DEFAULT_BYTES_PER_CYCLES)
``` [1](#0-0) 

`FeeRate::fee(weight)` computes `fee_rate × weight / 1000` in shannons. [2](#0-1) 

**Location 1 — `check_tx_fee`** (`tx-pool/src/util.rs:45`):

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [3](#0-2) 

`tx_size` is the serialized byte length of the transaction. It is passed directly to `FeeRate::fee()` as if it were `weight`. The comment acknowledges the mismatch but labels it a "cheap check." There is **no subsequent accurate weight-based fee-rate check** after `verify_rtx` returns the actual cycle count. The entry is created and admitted with the actual cycles, but the fee-rate gate has already been passed using only bytes.

**Location 2 — `calculate_min_replace_fee`** (`tx-pool/src/pool.rs:103`):

```rust
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
``` [4](#0-3) 

This is called from `check_rbf` (Rule #3/#4) to enforce the RBF minimum fee increment: [5](#0-4) 

Again, `size` (bytes) is used where `weight` is required. A cycle-heavy replacement transaction can satisfy the RBF fee check while paying far less than the intended minimum increment.

---

### Impact Explanation

**Concrete numbers** (default config: `min_fee_rate = 1000`, `max_tx_verify_cycles = 70_000_000`): [6](#0-5) 

- A small transaction (200 bytes) consuming 70,000,000 cycles has:
  - `weight = max(200, 70_000_000 × 0.000_170_571_4) ≈ max(200, 11_940) = 11_940`
  - Correct minimum fee: `1000 × 11_940 / 1000 = 11_940 shannons`
  - Actual check: `1000 × 200 / 1000 = 200 shannons`
  - **Attacker pays ~200 shannons instead of ~11,940 shannons — a ~60× reduction.**

An attacker can:
1. **Flood the tx-pool** with cycle-heavy transactions at a fraction of the required fee cost, displacing legitimate transactions.
2. **Bypass the RBF minimum increment**: a cycle-heavy replacement transaction can replace an existing transaction while paying far less than the required `min_rbf_rate`-based increment.
3. **Force expensive script verification** on the node for each submission, since `verify_rtx` runs the full CKB-VM execution, while the attacker's fee cost is computed only against byte size.

The eviction key does use actual weight (`get_transaction_weight`), so these transactions are evicted first when the pool is full — but the attacker can continuously resubmit them, each time forcing a full script verification at minimal fee cost. [7](#0-6) 

---

### Likelihood Explanation

This is reachable by any unprivileged user who can call `send_transaction` via the JSON-RPC API. No special role, key, or configuration is required. The attacker only needs to craft a transaction with a script that consumes near-maximum cycles (e.g., a tight loop in a CKB-VM script) while keeping the serialized transaction size small. This is straightforward to construct.

---

### Recommendation

Replace `tx_size as u64` with the actual `weight` in both locations. Since `check_tx_fee` is called before `verify_rtx` (cycles are not yet known), use the `max_tx_verify_cycles` as a conservative upper bound for the cycle contribution to weight, or restructure the flow to re-check the fee rate after `verify_rtx` returns the actual cycle count.

For `calculate_min_replace_fee`, the entry's actual cycles are already known (the entry is a `TxEntry` with a `cycles` field), so `get_transaction_weight(entry.size, entry.cycles)` should be used directly.

---

### Proof of Concept

1. Craft a CKB transaction with a lock script that loops for ~70,000,000 cycles and a small serialized size (~200 bytes).
2. Set the transaction fee to 200 shannons (satisfying `min_fee_rate.fee(200) = 200`).
3. Submit via `send_transaction` RPC.
4. The transaction passes `check_tx_fee` (200 ≥ 200), proceeds to `verify_rtx`, consumes ~70M cycles, and is admitted to the pool.
5. The actual fee rate is `FeeRate::calculate(200_shannons, 11_940_weight) ≈ 16 shannons/kilo-weight`, far below the configured minimum of 1000.
6. Repeat continuously to keep the pool filled with below-minimum-fee-rate cycle-heavy transactions, forcing the node to perform expensive script verification on each submission.

For the RBF variant: submit a legitimate transaction, then submit a cycle-heavy replacement with fee = `sum(replaced_fees) + min_rbf_rate.fee(size)` instead of the correct `sum(replaced_fees) + min_rbf_rate.fee(weight)`, bypassing Rule #3/#4 of the RBF check. [5](#0-4)

### Citations

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

**File:** tx-pool/src/pool.rs (L102-103)
```rust
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

**File:** resource/ckb.toml (L212-215)
```text
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
max_tx_verify_cycles = 70_000_000
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
