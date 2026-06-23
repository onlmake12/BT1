### Title
Minimum Fee Check Uses Only Serialized Size, Ignoring Cycle-Derived Weight — (`tx-pool/src/util.rs`)

---

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size. However, CKB's actual transaction weight is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. A transaction with small serialized size but high cycle consumption will have a true weight far exceeding its byte size, yet it passes the minimum fee check with a fee calibrated only to the byte size. This allows any unprivileged tx-pool submitter to inject transactions whose effective fee rate is orders of magnitude below the configured `min_fee_rate`.

---

### Finding Description

CKB's fee model uses a unified "weight" metric to convert the two-dimensional block resource constraint (bytes + cycles) into a single knapsack dimension:

```rust
// util/types/src/core/tx_pool.rs
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
// DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4
``` [1](#0-0) 

This weight is used everywhere for fee rate calculation — in `TxEntry::fee_rate()`, in the fee estimators, and in block assembly scoring:

```rust
// tx-pool/src/component/entry.rs
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [2](#0-1) 

However, the admission gate — `check_tx_fee` — computes the minimum required fee using **only `tx_size`**, not weight:

```rust
// tx-pool/src/util.rs
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [3](#0-2) 

The same size-only pattern appears in the RBF extra-fee calculation:

```rust
// tx-pool/src/pool.rs
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
``` [4](#0-3) 

**Concrete example with default `min_fee_rate = 1000` shannons/KW:**

| Parameter | Value |
|---|---|
| `tx_size` | 100 bytes |
| `cycles` | 70,000,000 (= `max_tx_verify_cycles`) |
| `weight` | `max(100, 70_000_000 × 0.000170571)` = **11,940** |
| `min_fee` (size-only check) | `1000 × 100 / 1000` = **100 shannons** |
| Effective fee rate after admission | `100 × 1000 / 11940` ≈ **8.4 shannons/KW** |

The transaction passes the gate paying 100 shannons, but its true fee rate is ~8.4 shannons/KW — **119× below** the configured minimum of 1000 shannons/KW. [5](#0-4) 

---

### Impact Explanation

An attacker can flood the tx-pool with transactions that carry a fee rate far below `min_fee_rate`. These transactions:

1. **Occupy tx-pool capacity** (up to `max_tx_pool_size = 180 MB`) at a fraction of the intended cost, displacing legitimate higher-fee transactions.
2. **Degrade miner revenue**: miners who include these transactions receive far less fee per unit of block resource consumed than the node operator's configured floor.
3. **Corrupt fee estimation**: the `FeeRateCollector` and `ConfirmationFraction`/`WeightUnitsFlow` estimators ingest these entries and will produce artificially low fee rate recommendations to wallets. [6](#0-5) 

---

### Likelihood Explanation

The attack requires only the ability to submit a transaction via the `send_transaction` RPC or via P2P relay — both are fully unprivileged. Crafting a transaction with small serialized size and high cycle consumption is straightforward: a script that runs a tight loop consumes many cycles while the transaction's on-chain footprint (inputs, outputs, witnesses) remains small. The default `max_tx_verify_cycles = 70,000,000` provides a large amplification factor. [7](#0-6) 

---

### Recommendation

Replace the size-only minimum fee computation in `check_tx_fee` with the full weight. Because cycles are not yet known at the point `check_tx_fee` is called (they are determined by script execution), the check should be deferred or repeated after cycle counting is complete. Concretely:

1. After `verify_rtx` returns the `Completed` entry (which contains the actual cycle count), recompute `min_fee` using `get_transaction_weight(tx_size, completed.cycles)` and reject if `fee < min_fee_rate.fee(weight)`.
2. Apply the same fix to `calculate_min_replace_fee` in `tx-pool/src/pool.rs`, using the replacing transaction's weight (size + cycles) rather than size alone. [8](#0-7) 

---

### Proof of Concept

1. Construct a CKB transaction with:
   - One input cell spending a live cell
   - One output cell (minimal capacity)
   - A lock script that executes a tight CKB-VM loop consuming ~70,000,000 cycles
   - Serialized size ≈ 100–200 bytes
2. Set the fee to `min_fee_rate * tx_size / 1000` shannons (e.g., 100–200 shannons at the default 1000 shannons/KB rate).
3. Submit via `send_transaction` RPC.
4. Observe the transaction is accepted into the pool despite its true fee rate being `fee * 1000 / get_transaction_weight(size, 70_000_000)` ≈ 8–17 shannons/KW, far below the 1000 shannons/KW minimum.
5. Repeat to fill the pool with sub-minimum-fee-rate transactions. [3](#0-2) [5](#0-4)

### Citations

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

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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

**File:** tx-pool/src/pool.rs (L103-103)
```rust
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```

**File:** rpc/src/util/fee_rate.rs (L86-111)
```rust
        let mut fee_rates = self.provider.collect(target, |mut fee_rates, block_ext| {
            let BlockExt {
                txs_sizes,
                cycles,
                txs_fees,
                ..
            } = block_ext;
            let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
            if txs_sizes.len() > 1 && !txs_fees.is_empty() {
                // block_ext.txs_fees's length == block_ext.cycles's length
                // block_ext.txs_fees's length + 1 == txs_sizes's length
                if let Some(cycles) = cycles {
                    for (fee, cycles, size) in itertools::izip!(
                        txs_fees,
                        cycles,
                        txs_sizes.iter().skip(1) // skip cellbase (first element in the Vec)
                    ) {
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
                    }
                }
            }
            fee_rates
        });
```

**File:** util/app-config/src/legacy/tx_pool.rs (L10-16)
```rust
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
// default max ancestors count
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
```
