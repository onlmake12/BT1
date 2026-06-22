### Title
Minimum Fee Check Uses Size Instead of Weight, Allowing High-Cycle Transactions Below Effective Fee Rate — (File: `tx-pool/src/util.rs`)

### Summary

The `check_tx_fee` function in the CKB tx-pool admission path computes the minimum required fee using only the serialized transaction **size**, not the transaction **weight** (`max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`). This is structurally identical to the Balancer bug: a rate/conversion factor (cycles-to-bytes ratio) is not applied before the comparison that gates admission, allowing transactions whose true weight-adjusted fee rate is far below the configured minimum to enter the pool.

---

### Finding Description

CKB defines transaction weight as:

```
weight = max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)
```

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (≈ `MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES`). [1](#0-0) 

`FeeRate` is defined in shannons per kilo-**weight** (KW), not per kilo-byte: [2](#0-1) 

However, the pool admission gate in `check_tx_fee` computes the minimum fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [3](#0-2) 

The correct weight is only applied later — for pool ordering and eviction — via `TxEntry::fee_rate()`: [4](#0-3) 

Because `check_tx_fee` does not receive the cycles count (cycles are unknown before script execution), no weight-based gate is applied at admission time. There is no post-execution weight-based fee re-check before the entry is stored in the pool.

---

### Impact Explanation

Consider a transaction with:
- `size = 242 bytes` (minimum realistic tx)
- `cycles = 70,000,000` (near `max_tx_verify_cycles`)
- `min_fee_rate = 1,000 shannons/KW`

| Metric | Value |
|---|---|
| Correct weight | `max(242, 70_000_000 × 0.000_170_571_4)` = **11,940** |
| Correct min fee | `1,000 × 11,940 / 1,000` = **11,940 shannons** |
| Size-based min fee (actual check) | `1,000 × 242 / 1,000` = **242 shannons** |
| Discount factor | **~49×** cheaper than the intended minimum |

An attacker can submit transactions that:
1. Pass the size-based minimum fee check with only 242 shannons
2. Force the node to execute 70M cycles of script verification
3. Enter the pool with an actual fee rate of ~20 shannons/KW — 50× below the configured minimum
4. Occupy pool slots until evicted, displacing legitimate transactions

This enables a resource-exhaustion attack: the node spends maximum verification cycles per transaction at a fraction of the intended cost. The pool's total size limit bounds the attack but does not prevent it from being effective.

---

### Likelihood Explanation

Any unprivileged RPC caller can submit transactions via `send_transaction`. Crafting a transaction with a computationally expensive script (e.g., a tight loop in CKB-VM) but small serialized size is straightforward. The attacker only needs to pay the size-based minimum fee, which is publicly documented and trivially computable. No special access, key material, or majority hashpower is required.

---

### Recommendation

After script execution returns the actual cycles, perform a second weight-based fee check before admitting the entry to the pool:

```rust
// After verify_rtx returns completed cycles:
let weight = get_transaction_weight(tx_size, completed.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

This mirrors how `TxEntry::fee_rate()` and `AncestorsScoreSortKey` already correctly apply `get_transaction_weight` for ordering: [5](#0-4) 

---

### Proof of Concept

1. Craft a CKB transaction whose lock or type script runs a tight computation loop consuming ~70M cycles, with a small serialized body (~242 bytes).
2. Set the transaction fee to 242 shannons (exactly the size-based minimum at 1,000 shannons/KW).
3. Submit via `send_transaction` RPC.
4. Observe: the node executes 70M cycles of script verification and admits the transaction to the pool. The actual fee rate is `FeeRate::calculate(242 shannons, 11940 weight)` ≈ 20 shannons/KW — 50× below the configured minimum.
5. Repeat to fill the pool with high-cycle, low-fee entries, displacing legitimate transactions and consuming node verification resources at ~1/50th the intended cost. [6](#0-5) [7](#0-6)

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

**File:** util/types/src/core/fee_rate.rs (L3-16)
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
```

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
