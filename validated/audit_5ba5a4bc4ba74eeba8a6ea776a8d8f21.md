### Title
Tx-Pool `min_fee_rate` Admission Check Uses Serialized Size Instead of Weight, Allowing Bypass via High-Cycle Transactions — (File: `tx-pool/src/util.rs`)

### Summary

The `check_tx_fee` function enforces `min_fee_rate` using only the transaction's serialized byte size as the weight denominator. However, once the transaction is verified and admitted, its actual effective fee rate is computed using `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For any transaction whose cycle count is large enough that `cycles × DEFAULT_BYTES_PER_CYCLES > size`, the actual weight exceeds the size used at admission, and the transaction's true fee rate falls below `min_fee_rate`. The minimum guarantee check passes, but the actual enforced rate is lower — a direct structural analog to the BarnBridge `minGain_` bypass.

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` computes the minimum required fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { return Err(Reject::LowFeeRate(...)); }
``` [1](#0-0) 

This check is called during `pre_check`, before `verify_rtx` has run and before actual cycles are known:

```rust
let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
``` [2](#0-1) 

After `verify_rtx` completes, the `TxEntry` is created with the actual verified cycles:

```rust
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
``` [3](#0-2) 

The entry's actual fee rate is then computed using `get_transaction_weight`:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [4](#0-3) 

Where `get_transaction_weight` is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
``` [5](#0-4) 

With `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`, the weight exceeds the size whenever `cycles > size / 0.000_170_571_4 ≈ size × 5862`. For a 1000-byte transaction, this threshold is ~5.86M cycles — well within the default `max_tx_verify_cycles = 70_000_000`.

**Concrete example:**
- `tx_size = 1000` bytes, `cycles = 70_000_000`, `min_fee_rate = 1000 shannons/KW`
- Admission check: `min_fee = 1000 × 1000 / 1000 = 1000 shannons` → passes with `fee = 1000`
- Actual weight: `max(1000, 70_000_000 × 0.000_170_571_4) = max(1000, 11940) = 11940`
- Actual fee rate: `1000 × 1000 / 11940 = 83 shannons/KW` — **12× below `min_fee_rate`**

### Impact Explanation

Any unprivileged tx-pool submitter (via RPC `send_transaction`) or relay peer can craft a transaction with a script that consumes many cycles but has a small serialized size. Such a transaction:

1. **Bypasses `min_fee_rate` enforcement**: The node operator's configured minimum fee rate is not actually enforced for high-cycle transactions.
2. **Pollutes fee estimation**: The `estimate_fee_rate` fallback in `pool_map.rs` iterates entries by `fee_rate()` (weight-based). Admitted low-effective-fee-rate entries skew the estimate downward, misleading honest users about the fee required for timely confirmation.
3. **Wastes pool resources**: The transaction occupies pool space until evicted, consuming verification CPU (up to `max_tx_verify_cycles`) and pool memory. [6](#0-5) 

### Likelihood Explanation

The entry path is fully open to any RPC caller or relay peer. No special privilege is required. A script author can trivially write a RISC-V script that loops to consume cycles without increasing transaction size. The discrepancy grows linearly with cycles, so the bypass is maximally effective at `max_tx_verify_cycles`.

### Recommendation

Replace the size-only proxy in `check_tx_fee` with a weight-based check. Since cycles are not yet known at pre-check time, use the declared cycles (for remote transactions) or a conservative upper bound (e.g., `max_tx_verify_cycles`) to compute a weight-based minimum fee:

```rust
// Use declared_cycles if available, else use max_tx_verify_cycles as upper bound
let weight = get_transaction_weight(tx_size, declared_cycles.unwrap_or(max_tx_verify_cycles));
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

This ensures the admitted transaction's effective fee rate is at or above `min_fee_rate` regardless of actual cycle count.

### Proof of Concept

1. Deploy a CKB script that loops for ~60M cycles (e.g., a tight RISC-V loop), keeping the script binary small (~200 bytes).
2. Construct a transaction spending a live cell, referencing the script as a lock, with `tx_size ≈ 1000` bytes.
3. Set the transaction fee to exactly `min_fee_rate × tx_size / 1000 = 1000 shannons` (the size-based minimum).
4. Submit via `send_transaction` RPC to a node with `min_fee_rate = 1000`.
5. Observe: the transaction is accepted into the pool.
6. Query `get_raw_tx_pool` and compute `fee / get_transaction_weight(size, cycles)` — the actual fee rate is ~83 shannons/KW, far below `min_fee_rate = 1000`.
7. Repeat with many such transactions; observe `estimate_fee_rate` returning artificially low values. [7](#0-6) [8](#0-7)

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

**File:** tx-pool/src/process.rs (L289-290)
```rust
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
```

**File:** tx-pool/src/process.rs (L751-751)
```rust
        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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
