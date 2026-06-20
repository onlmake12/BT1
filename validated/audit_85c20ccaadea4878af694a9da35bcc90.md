### Title
Tx-Pool Minimum Fee Rate Admission Check Uses Serialized Size Instead of Full Transaction Weight, Allowing High-Cycles Transactions to Bypass Fee Enforcement - (`tx-pool/src/util.rs`)

---

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` computes the minimum required fee using only the transaction's serialized byte size (`tx_size`) as the weight, rather than the full transaction weight (`max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`). This allows a transaction submitter to craft a transaction with a small serialized size but very high execution cycles, paying a fee far below what the minimum fee rate actually requires for that transaction's true resource cost. The fee is "computed" against an underestimated weight but never "charged" against the real weight — a direct structural analog to the GTE Uniswap contract computing a fee-adjusted balance without actually deducting the fee.

---

### Finding Description

In `tx-pool/src/util.rs`, the admission gate for the tx-pool is:

```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(...)
        .transaction_fee(rtx)...?;
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    if fee < min_fee {
        return Err(Reject::LowFeeRate(...));
    }
    Ok(fee)
}
``` [1](#0-0) 

The comment itself acknowledges the problem: `"Theoretically we cannot use size as weight directly to calculate fee_rate"`. The correct weight is defined in `util/types/src/core/tx_pool.rs`:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. For a transaction with high cycles, the cycle-derived weight dominates and can be orders of magnitude larger than the serialized size.

The `TxEntry::fee_rate()` method — used for pool prioritization — correctly uses the full weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [3](#0-2) 

But there is **no subsequent admission check** that re-validates the fee rate using the full weight after cycles are known from script execution. The only admission gate is `check_tx_fee`, which uses `tx_size` alone.

The `FeeRate::fee()` function that computes `min_fee`:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;
    Capacity::shannons(fee)
}
``` [4](#0-3) 

When called with `tx_size` instead of the full weight, it produces a `min_fee` that is far too small for high-cycles transactions.

---

### Impact Explanation

An attacker submits a transaction with:
- Serialized size: ~1,000 bytes
- Execution cycles: ~3.5 × 10⁹ (near `max_block_cycles`)
- Fee: just above `min_fee_rate × 1000 / 1000` shannons

The admission check passes because `fee ≥ min_fee_rate × tx_size`. However, the actual weight is `max(1000, 3.5×10⁹ × 0.000170571) ≈ 597,000 bytes`. The required fee at the correct weight would be ~597× larger. The attacker pays ~0.17% of the fee that should be required.

Consequences:
1. **Tx-pool space exhaustion**: The attacker can fill the tx-pool with high-cycles, low-fee-rate entries at a fraction of the legitimate cost, evicting honest transactions.
2. **Miner resource waste**: If miners include such transactions (e.g., during low-traffic periods), they consume disproportionate block cycle budget for minimal fee income — miners lose fee revenue relative to the cycles consumed.
3. **Fee market distortion**: The `estimate_fee_rate` fallback algorithm reads `entry.inner.fee_rate()` (full weight) from pool entries; flooding the pool with artificially low-fee-rate entries skews fee estimates downward for all users. [5](#0-4) 

---

### Likelihood Explanation

- **Entry path**: Any unprivileged tx-pool submitter via the `send_transaction` RPC or P2P relay. No special role required.
- **Cost**: Non-zero but far below the correct minimum. The attacker must pay `min_fee_rate × tx_size` shannons per transaction, which is a small fraction of the correct cost for high-cycles transactions.
- **Feasibility**: Crafting a valid transaction with high cycles is straightforward — any script that loops near the cycle limit suffices. The attacker controls the script content.
- **No trusted role required**: Standard tx submission path.

---

### Recommendation

Replace the size-only weight in `check_tx_fee` with the full transaction weight. Since cycles are not yet known at the pre-script-execution admission stage, two options exist:

1. **Two-phase check**: Perform a size-only pre-check for fast rejection, then after script execution (when cycles are known), re-validate `fee ≥ min_fee_rate.fee(get_transaction_weight(tx_size, cycles))` before inserting into the pool map.
2. **Conservative cycle estimate**: Use a per-byte cycle budget to derive a conservative upper-bound cycle count from `tx_size` for the pre-check, ensuring the pre-check is never more permissive than the full check.

The fix should be applied in `tx-pool/src/util.rs` at `check_tx_fee`, and the post-script-execution insertion path should enforce the full-weight fee rate check.

---

### Proof of Concept

1. Craft a CKB transaction whose lock or type script loops for ~3 × 10⁹ cycles (near `max_block_cycles`). Serialized transaction size: ~1,000 bytes.
2. Set `outputs_capacity = inputs_capacity − 1001 shannons` (fee = 1001 shannons).
3. Submit via `send_transaction` RPC to a node with `min_fee_rate = 1000 shannons/KW`.
4. **Admission check** (`check_tx_fee`): `min_fee = 1000 × 1000 / 1000 = 1000 shannons`. Fee (1001) ≥ min_fee (1000). **Transaction admitted.**
5. After script execution, cycles ≈ 3 × 10⁹. Full weight ≈ `max(1000, 3×10⁹ × 0.000170571)` ≈ 511,713 bytes. Correct min_fee = `1000 × 511713 / 1000 = 511,713 shannons`. **No re-check is performed.**
6. The transaction occupies a pool slot with an effective fee rate of `1001 × 1000 / 511713 ≈ 1.96 shannons/KW` — 510× below the minimum — and is never evicted by the fee-rate check. [6](#0-5) [7](#0-6)

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

**File:** util/types/src/core/tx_pool.rs (L279-303)
```rust
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

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** tx-pool/src/component/pool_map.rs (L334-358)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        mut target_blocks: usize,
        max_block_bytes: usize,
        max_block_cycles: Cycle,
        min_fee_rate: FeeRate,
    ) -> FeeRate {
        debug_assert!(target_blocks > 0);
        let iter = self.entries.iter_by_score().rev();
        let mut current_block_bytes = 0;
        let mut current_block_cycles = 0;
        for entry in iter {
            current_block_bytes += entry.inner.size;
            current_block_cycles += entry.inner.cycles;
            if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
                target_blocks -= 1;
                if target_blocks == 0 {
                    return entry.inner.fee_rate();
                }
                current_block_bytes = entry.inner.size;
                current_block_cycles = entry.inner.cycles;
            }
        }

        min_fee_rate
```
