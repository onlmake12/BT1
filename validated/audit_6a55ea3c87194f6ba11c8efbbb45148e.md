### Title
RBF Minimum Replacement Fee Calculated Using Raw Byte Size Instead of Transaction Weight — (`tx-pool/src/pool.rs`)

---

### Summary

`calculate_min_replace_fee` in `tx-pool/src/pool.rs` passes the raw serialized byte size of the replacement transaction to `FeeRate::fee()`, which expects a **weight** value (the cycle-adjusted unit used throughout the fee-rate system). For cycle-heavy transactions, weight can greatly exceed raw size, so the enforced minimum replacement fee is systematically underestimated. This is the direct CKB analog of the reported slippage bug: a value in one unit (bytes) is used where a different unit (weight) is required.

---

### Finding Description

`FeeRate` is defined as **shannons per kilo-weight**, where weight is computed by `get_transaction_weight`:

```rust
// util/types/src/core/tx_pool.rs
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [1](#0-0) 

`FeeRate::fee(weight)` converts a weight value to a fee amount:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;
    Capacity::shannons(fee)
}
``` [2](#0-1) 

Every other fee-rate calculation in the codebase correctly calls `get_transaction_weight(size, cycles)` before passing the result to `FeeRate::calculate` or `FeeRate::fee`. For example, `TxEntry::fee_rate()`:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [3](#0-2) 

However, `calculate_min_replace_fee` skips the weight conversion and passes raw bytes directly:

```rust
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
    let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);  // ← size, not weight
    ...
}
``` [4](#0-3) 

This function is called from two places, both passing only `size` with no `cycles` argument:

1. **`check_rbf`** — the enforcement path that decides whether to accept or reject a replacement transaction:

```rust
if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
``` [5](#0-4) 

2. **`min_replace_fee`** — the RPC-facing query that tells callers how much fee is required:

```rust
self.calculate_min_replace_fee(&conflicts, tx.size)
``` [6](#0-5) 

The `min_replace_fee` value is surfaced to external callers via `TransactionWithStatus` and the `get_transaction` RPC. [7](#0-6) 

---

### Impact Explanation

For a cycle-heavy replacement transaction where `cycles * DEFAULT_BYTES_PER_CYCLES > tx_size`, the true weight is much larger than the raw byte size. The `extra_rbf_fee` component of the minimum replacement fee is therefore underestimated by a factor of up to `weight / size`.

- **Enforcement bypass**: An attacker can submit a cycle-heavy replacement transaction that satisfies the underestimated `min_replace_fee` check in `check_rbf`, even though its true weight-based fee rate is below the intended `min_rbf_rate` threshold. This undermines the economic guarantee of RBF: that a replacement transaction must pay a meaningfully higher fee rate.
- **Incorrect RPC guidance**: The `min_replace_fee` field returned by `get_transaction` is too low for cycle-heavy transactions, misleading wallets and users about the actual fee required.

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`, and `MAX_BLOCK_CYCLES` is on the order of `3.5 × 10^9`. A transaction consuming the full cycle budget would have a weight of ~597,000 weight units, while its serialized size could be as small as a few hundred bytes — a ratio exceeding 1000×. [8](#0-7) 

---

### Likelihood Explanation

RBF is enabled by default when `min_rbf_rate > min_fee_rate` (default config: 1500 vs 1000 shannons/KW). Any unprivileged transaction sender can craft a cycle-heavy transaction and submit it via the `send_transaction` RPC. No special privileges, keys, or majority hashpower are required. The attacker only needs to construct a transaction whose script execution consumes many cycles while keeping the serialized transaction small. [9](#0-8) 

---

### Recommendation

Replace the raw `size` argument with the proper weight in `calculate_min_replace_fee`. The function signature should accept both `size` and `cycles`, compute weight internally, and pass it to `FeeRate::fee`:

```rust
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize, cycles: Cycle) -> Option<Capacity> {
    let weight = get_transaction_weight(size, cycles);
    let extra_rbf_fee = self.config.min_rbf_rate.fee(weight);
    ...
}
```

Both call sites (`check_rbf` and `min_replace_fee`) must be updated to pass `entry.cycles` alongside `entry.size`.

---

### Proof of Concept

1. Node has RBF enabled (`min_rbf_rate = 1500`, `min_fee_rate = 1000`).
2. Attacker submits transaction T1 (small, normal cycles) with fee F1.
3. Attacker crafts replacement T2 with the same inputs, a script that consumes ~500,000,000 cycles, and a serialized size of ~300 bytes.
   - True weight: `max(300, 500_000_000 * 0.000_170_571_4)` ≈ 85,285 weight units.
   - Correct `extra_rbf_fee` at 1500 shannons/KW: `1500 * 85285 / 1000` = **127,927 shannons**.
   - Actual `extra_rbf_fee` computed by buggy code: `1500 * 300 / 1000` = **450 shannons**.
4. T2 is accepted by `check_rbf` with `fee >= F1 + 450` shannons, even though the correct threshold is `F1 + 127,927` shannons.
5. The `get_transaction(T1_hash)` RPC also returns `min_replace_fee = F1 + 450`, misleading any wallet that queries it. [10](#0-9) [1](#0-0) [2](#0-1)

### Citations

**File:** util/types/src/core/tx_pool.rs (L195-213)
```rust
impl TransactionWithStatus {
    /// Build with tx status
    pub fn with_status(
        tx: Option<core::TransactionView>,
        cycles: core::Cycle,
        time_added_to_pool: u64,
        tx_status: TxStatus,
        fee: Option<Capacity>,
        min_replace_fee: Option<Capacity>,
    ) -> Self {
        Self {
            tx_status,
            fee,
            min_replace_fee,
            transaction: tx,
            cycles: Some(cycles),
            time_added_to_pool: Some(time_added_to_pool),
        }
    }
```

**File:** util/types/src/core/tx_pool.rs (L279-279)
```rust
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

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/pool.rs (L81-83)
```rust
    pub fn enable_rbf(&self) -> bool {
        self.config.min_rbf_rate > self.config.min_fee_rate
    }
```

**File:** tx-pool/src/pool.rs (L98-98)
```rust
        self.calculate_min_replace_fee(&conflicts, tx.size)
```

**File:** tx-pool/src/pool.rs (L101-127)
```rust
    /// min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
        // don't account for duplicate txs
        let replaced_fees: HashMap<_, _> = conflicts
            .iter()
            .map(|c| (c.id.clone(), c.inner.fee))
            .collect();
        let replaced_sum_fee = replaced_fees
            .values()
            .try_fold(Capacity::zero(), |acc, x| acc.safe_add(*x));
        let res = replaced_sum_fee.map_or(Err(CapacityError::Overflow), |sum| {
            sum.safe_add(extra_rbf_fee)
        });
        if let Ok(res) = res {
            Some(res)
        } else {
            let fees = conflicts.iter().map(|c| c.inner.fee).collect::<Vec<_>>();
            error!(
                "conflicts: {:?} replaced_sum_fee {:?} overflow by add {}",
                conflicts.iter().map(|e| e.id.clone()).collect::<Vec<_>>(),
                fees,
                extra_rbf_fee
            );
            None
        }
    }
```

**File:** tx-pool/src/pool.rs (L665-665)
```rust
        if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
```
