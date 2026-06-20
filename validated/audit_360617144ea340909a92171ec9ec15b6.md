### Title
Minimum Fee Rate Admission Check Uses Raw `tx_size` Instead of `weight`, Allowing Cycle-Heavy Transactions to Bypass Fee Rate Enforcement — (File: `tx-pool/src/util.rs`)

---

### Summary

In `check_tx_fee`, the minimum required fee is computed using raw `tx_size` as the weight parameter. However, the actual fee rate of a transaction is computed using `get_transaction_weight(size, cycles)` = `max(size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions, this asymmetry — analogous to the CurveFi `xs`/`ys` rate mismatch — allows transactions with actual fee rates far below the configured minimum to be admitted to the mempool by any unprivileged RPC caller.

---

### Finding Description

In `tx-pool/src/util.rs` at line 45, `check_tx_fee` computes the minimum required fee as:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

`FeeRate::fee` is defined as `self.0.saturating_mul(weight) / KW`, so this computes `min_fee_rate * tx_size / 1000`. The code comment at line 42 explicitly acknowledges the inconsistency: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly."* [1](#0-0) 

However, the actual fee rate of every admitted `TxEntry` is computed in `tx-pool/src/component/entry.rs` at line 116 using the full weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [2](#0-1) 

`get_transaction_weight` is defined in `util/types/src/core/tx_pool.rs`:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [3](#0-2) 

Where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [4](#0-3) 

The same `weight`-based calculation is used consistently everywhere else: in `FeeRateCollector::statistics` for historical fee rate reporting, in `TxEntry::as_score_key` for pool ordering, and in `EvictKey` for eviction decisions. [5](#0-4) [6](#0-5) 

The asymmetry is structural: the **admission gate** (`check_tx_fee`) uses `tx_size`, while the **actual fee rate** used for ordering and eviction uses `weight = max(size, cycles * factor)`. For cycle-heavy transactions, `weight >> tx_size`, so the admission gate is far less strict than the actual fee rate calculation — directly analogous to the CurveFi `xs` (without rates) vs. `ys` (with rates) mismatch.

---

### Impact Explanation

An unprivileged transaction submitter (any `send_transaction` RPC caller) can craft a cycle-heavy transaction that passes `check_tx_fee` while having an actual fee rate far below `min_fee_rate`.

Concrete example with default configuration (`min_fee_rate = 1000 shannons/KW`, `max_tx_verify_cycles = 70,000,000`):

| Parameter | Value |
|---|---|
| `tx_size` | 200 bytes |
| `cycles` | 70,000,000 |
| `fee` | 200 shannons |
| `min_fee` check (`size`-based) | `1000 * 200 / 1000 = 200` → **passes** |
| Actual `weight` | `max(200, 70000000 × 0.000170571) ≈ 11,940` |
| Actual fee rate | `200 × 1000 / 11940 ≈ 16.7 shannons/KW` |
| Minimum fee rate | `1000 shannons/KW` |
| **Bypass factor** | **~60×** |

Such transactions are admitted to the mempool with actual fee rates ~60× below the minimum. The pool size is measured in bytes (`max_tx_pool_size = 180 MB`). At 200 bytes per transaction, an attacker needs ~900,000 transactions at 200 shannons each ≈ **1.8 CKB** to fill the pool, displacing legitimate transactions. [7](#0-6) 

---

### Likelihood Explanation

High. The entry path is the standard `send_transaction` RPC, reachable by any unprivileged peer or RPC caller. No special privileges, keys, or majority hashpower are required. The attack is economically cheap (sub-2 CKB to fill the pool) and mechanically straightforward: submit transactions with high `cycles` and minimal `fee`. The `max_tx_verify_cycles` limit bounds the maximum bypass factor but does not eliminate it.

---

### Recommendation

Replace `tx_size as u64` with `get_transaction_weight(tx_size, cycles)` in `check_tx_fee` so the admission gate is consistent with the actual fee rate calculation used everywhere else. The `cycles` value is available after script verification and should be threaded into `check_tx_fee`. This mirrors the fix analogous to the CurveFi recommendation of multiplying `xs` by `rates` to restore symmetry.

```rust
// Before (line 45):
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);

// After:
let weight = get_transaction_weight(tx_size, cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
``` [8](#0-7) 

---

### Proof of Concept

1. Craft a transaction with `size ≈ 200 bytes`, `cycles = 70,000,000`, and `fee = 200 shannons`.
2. Submit via `send_transaction` RPC.
3. `check_tx_fee` computes `min_fee = 1000 * 200 / 1000 = 200 shannons`; `fee (200) >= min_fee (200)` → **admitted**.
4. Actual fee rate: `200 * 1000 / max(200, 11940) ≈ 16.7 shannons/KW` — ~60× below the 1000 shannons/KW minimum.
5. Repeat ~900,000 times (~1.8 CKB total) to fill the 180 MB pool with transactions whose actual fee rates are far below the minimum, displacing legitimate higher-fee-rate transactions and degrading mempool utility. [9](#0-8) [10](#0-9)

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/component/entry.rs (L221-231)
```rust
impl From<&TxEntry> for AncestorsScoreSortKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let ancestors_weight = get_transaction_weight(entry.ancestors_size, entry.ancestors_cycles);
        AncestorsScoreSortKey {
            fee: entry.fee,
            weight,
            ancestors_fee: entry.ancestors_fee,
            ancestors_weight,
        }
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

**File:** rpc/src/util/fee_rate.rs (L98-106)
```rust
                    for (fee, cycles, size) in itertools::izip!(
                        txs_fees,
                        cycles,
                        txs_sizes.iter().skip(1) // skip cellbase (first element in the Vec)
                    ) {
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```
