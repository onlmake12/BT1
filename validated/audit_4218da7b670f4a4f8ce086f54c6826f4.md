### Title
Fee-Rate Unit Mismatch in `calculate_min_replace_fee` Allows RBF Fee-Bump Policy Bypass for High-Cycles Transactions — (`tx-pool/src/pool.rs`)

---

### Summary

`FeeRate` in CKB is defined as **shannons per kilo-weight**, where `weight = max(tx_size_bytes, cycles × DEFAULT_BYTES_PER_CYCLES)`. Two production functions — `calculate_min_replace_fee` and `check_tx_fee` — pass raw `tx_size` (bytes) as the weight argument to `FeeRate::fee()`, silently assuming that size always dominates weight. When a transaction's cycle count is large enough that `cycles × DEFAULT_BYTES_PER_CYCLES > tx_size`, the weight unit assumption is violated and the computed minimum fee is underestimated by up to ~60×. An unprivileged `send_transaction` RPC caller can exploit this to replace pool transactions while paying a fee increment far below the `min_rbf_rate` policy, and to admit transactions whose effective fee rate is far below `min_fee_rate`.

---

### Finding Description

**Unit definitions:**

`FeeRate` is "shannons per kilo-weight": [1](#0-0) 

`weight` is defined as: [2](#0-1) 

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`, so at `max_tx_verify_cycles = 70,000,000` cycles, the cycle-derived weight is `70,000,000 × 0.000_170_571_4 ≈ 11,940` weight-units. A minimal transaction can be as small as ~200 bytes, giving a discrepancy factor of ~60×.

**Root cause 1 — `calculate_min_replace_fee` (RBF path):**

```rust
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
    let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);  // ← size, not weight
``` [3](#0-2) 

`FeeRate::fee(weight)` computes `fee = rate × weight / 1000`. Passing `size` instead of `get_transaction_weight(size, cycles)` produces a result that is correct only when size dominates. For a 200-byte transaction consuming 70 M cycles, the correct extra fee at `min_rbf_rate = 1500` would be `1500 × 11940 / 1000 = 17,910 shannons`, but the code computes `1500 × 200 / 1000 = 300 shannons` — a 60× underestimate.

**Root cause 2 — `check_tx_fee` (admission path):**

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [4](#0-3) 

The comment acknowledges the mismatch but calls it a "cheap check". However, this is the **only** fee-rate gate before pool admission. After script execution the entry's `fee_rate()` uses the correct combined weight: [5](#0-4) 

There is no second fee-rate check after cycles are known. A high-cycles transaction admitted via the size-only check enters the pool with an effective fee rate far below `min_fee_rate`.

**`FeeRate::fee()` for reference:** [6](#0-5) 

---

### Impact Explanation

**RBF bypass (primary):** With RBF enabled (`min_rbf_rate > min_fee_rate`), an attacker crafts a replacing transaction with small serialized size but a complex lock script consuming near-maximum cycles. `calculate_min_replace_fee` computes the required fee bump using size-only weight, so the attacker pays ~1/60th of the intended extra fee. The replacing transaction is accepted into the pool with an effective fee rate far below `min_rbf_rate`, displacing the victim transaction while violating the fee-bump policy. This can be used to grief specific transactions — replacing them with a low-fee-rate substitute that miners will not prioritize, indefinitely delaying confirmation. [7](#0-6) 

**Pool admission bypass (secondary):** Any `send_transaction` caller can submit a high-cycles, small-size transaction with a fee just above `min_fee_rate.fee(size)`. The transaction is admitted to the pool with an effective fee rate potentially 60× below `min_fee_rate`. If the pool is not full, it persists indefinitely and is never mined, wasting pool slots and consuming significant CPU during script verification. [8](#0-7) 

---

### Likelihood Explanation

- RBF is enabled by default when `min_rbf_rate > min_fee_rate` (the shipped `ckb.toml` sets `min_fee_rate = 1_000` and `min_rbf_rate = 1_500`). [9](#0-8) 
- The attack requires only an unprivileged `send_transaction` RPC call — no special role, key, or majority hashpower.
- Crafting a transaction with a complex lock script that consumes many cycles is straightforward; CKB-VM supports arbitrary RISC-V programs.
- `max_tx_verify_cycles = 70,000,000` is the only upper bound on cycles, and it is large enough to produce a ~60× weight discrepancy for a minimal-size transaction. [10](#0-9) 

---

### Recommendation

Replace the raw `size` argument in both `calculate_min_replace_fee` and `check_tx_fee` with `get_transaction_weight(size, cycles)`. For `check_tx_fee`, cycles are not yet known at call time; the fix is to perform the fee-rate check **after** script execution (when cycles are available), or to store the cycles from the cache entry and pass them through. For `calculate_min_replace_fee`, the replacing entry's cycles are available in `TxEntry::cycles` and should be used:

```rust
// In calculate_min_replace_fee, receive cycles alongside size:
let weight = get_transaction_weight(size, cycles);
let extra_rbf_fee = self.config.min_rbf_rate.fee(weight);
```

The `get_transaction_weight` function is already imported and used correctly elsewhere in the codebase: [2](#0-1) 

---

### Proof of Concept

1. **Setup:** Node with default config (`min_fee_rate = 1000`, `min_rbf_rate = 1500`).

2. **Submit victim tx1:** Normal transaction, serialized size ≈ 200 bytes, cycles ≈ 1,000, fee = 200 shannons. Effective fee rate = `200 × 1000 / 200 = 1000 shannons/KW` (passes).

3. **Craft attacker tx2:** Same inputs as tx1 (conflict), lock script is a RISC-V loop consuming 70,000,000 cycles, serialized size ≈ 200 bytes.
   - `check_tx_fee`: `min_fee = 1000 × 200 / 1000 = 200 shannons`. Pay 501 shannons → passes.
   - `calculate_min_replace_fee`: `extra_rbf_fee = 1500 × 200 / 1000 = 300 shannons`. `min_replace_fee = 200 + 300 = 500 shannons`. Pay 501 shannons → passes RBF check.

4. **Actual weight of tx2:** `max(200, 70,000,000 × 0.000_170_571_4) = max(200, 11,940) = 11,940`.
   - Effective fee rate of tx2 = `501 × 1000 / 11,940 ≈ 41 shannons/KW`.
   - This is 24× below `min_fee_rate` and 36× below `min_rbf_rate`.

5. **Result:** tx1 is evicted from the pool. tx2 occupies the pool with an effective fee rate of ~41 shannons/KW. Miners sort by fee rate and will not include tx2 for a very long time (or never), indefinitely delaying the original transaction.

The correct extra RBF fee should have been `1500 × 11,940 / 1000 = 17,910 shannons`, not 300 shannons. The attacker paid 300 shannons instead of 17,910 — a 60× shortfall — due to the unit mismatch between `size` and `weight`.

### Citations

**File:** util/types/src/core/fee_rate.rs (L3-7)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;
```

**File:** util/types/src/core/fee_rate.rs (L33-37)
```rust
    /// Calculates the fee for a given weight.
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** resource/ckb.toml (L211-214)
```text
max_tx_pool_size = 180_000_000 # 180mb
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
```

**File:** resource/ckb.toml (L215-215)
```text
max_tx_verify_cycles = 70_000_000
```
