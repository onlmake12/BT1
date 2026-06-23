### Title
Min-Fee-Rate Check Uses Only Serialized Size, Ignoring Cycle-Based Weight — Cycle-Heavy Transactions Bypass Effective Fee Floor - (File: `tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size. CKB's actual fee-rate metric is `weight = max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A transaction with a small serialized size but near-maximum cycle consumption can pass the size-only gate with a fee that is a fraction of the true minimum, polluting the tx-pool and wasting verification resources. The code even documents this gap with an explicit comment.

---

### Finding Description

`check_tx_fee` is the sole admission-time fee-rate gate in the tx-pool: [1](#0-0) 

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

The correct weight formula, used everywhere else in the codebase (sorting, eviction, fee estimation), is: [2](#0-1) 

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [3](#0-2) 

`TxEntry::fee_rate()` and `AncestorsScoreSortKey` both call `get_transaction_weight` with both `size` and `cycles`: [4](#0-3) 

But `check_tx_fee` is called **before** script execution, so cycles are not yet available, and only `tx_size` is passed. No subsequent post-verification fee-rate re-check using the actual cycles is performed.

---

### Impact Explanation

**Concrete example** using default config values (`min_fee_rate = 1000 shannons/KW`, `max_tx_verify_cycles = 70_000_000`):

| Metric | Value |
|---|---|
| `tx_size` | 597 bytes (a typical 2-in-2-out tx) |
| `cycles` | 70,000,000 (max allowed per tx) |
| Size-based `min_fee` | `1000 × 597 / 1000 = 597 shannons` |
| Correct `weight` | `max(597, 70_000_000 × 0.000_170_571_4) = 11,940` |
| Weight-based `min_fee` | `1000 × 11,940 / 1000 = 11,940 shannons` |
| Effective fee rate at 597 shannons | `597 × 1000 / 11,940 ≈ 50 shannons/KW` |

An attacker pays **597 shannons** (passes the size-only gate) while the correct minimum is **11,940 shannons**. The effective fee rate is ~50 shannons/KW — **20× below** the configured floor of 1,000 shannons/KW.

Consequences:
1. **Tx-pool pollution**: cycle-heavy, low-fee-rate transactions are admitted and occupy pool slots, potentially evicting legitimate transactions when the pool is full.
2. **Wasted verification resources**: the node executes up to 70M cycles of script work per such transaction before discovering the true fee rate.
3. **Peer relay**: the transaction is relayed to peers, who also waste resources verifying it.
4. **Mining starvation**: admitted transactions sit at the bottom of the fee-rate priority queue and may never be mined, yet they permanently occupy pool space until expiry. [5](#0-4) 

---

### Likelihood Explanation

**Medium.** Any unprivileged RPC caller can submit a transaction via `send_transaction`. Crafting a transaction with a small serialized body (e.g., a single input/output with a computationally expensive lock script) and paying only the size-based minimum fee is straightforward. The attacker needs no special privileges, no key material beyond their own, and no majority hash power. [6](#0-5) 

---

### Recommendation

After script execution completes and cycles are known, perform a second fee-rate check using the correct weight:

```rust
// Post-verification check with actual cycles
let weight = get_transaction_weight(tx_size, cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

This mirrors how `TxEntry::fee_rate()` and `AncestorsScoreSortKey` already compute the effective fee rate, and closes the gap between the admission gate and the actual scheduling metric. [7](#0-6) 

---

### Proof of Concept

1. Craft a transaction with:
   - A lock script that consumes ~70,000,000 cycles (near `max_tx_verify_cycles`).
   - Serialized size of ~597 bytes.
   - Fee = 597 shannons (exactly `min_fee_rate.fee(597)`).

2. Submit via `send_transaction` RPC.

3. `check_tx_fee` computes `min_fee = 1000 × 597 / 1000 = 597 shannons`. Fee equals min_fee → **admitted**.

4. After script execution, actual weight = `max(597, 70_000_000 × 0.000_170_571_4) = 11,940`. Effective fee rate = `597 × 1000 / 11,940 ≈ 50 shannons/KW` — far below the 1,000 shannons/KW floor.

5. Transaction sits in the pool, consuming a slot, relayed to peers, and never mined due to its low effective fee rate. [8](#0-7) [9](#0-8)

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

**File:** util/app-config/src/configs/tx_pool.rs (L11-17)
```rust
pub struct TxPoolConfig {
    /// Keep the transaction pool below <max_tx_pool_size> mb
    pub max_tx_pool_size: usize,
    /// txs with lower fee rate than this will not be relayed or be mined
    #[serde(with = "FeeRateDef")]
    pub min_fee_rate: FeeRate,
    /// txs need to pay larger fee rate than this for RBF
```
