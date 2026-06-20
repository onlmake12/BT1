### Title
`check_tx_fee` Uses Serialized Size as Weight Instead of `get_transaction_weight`, Allowing Cycle-Heavy Transactions to Bypass `min_fee_rate` Enforcement — (`tx-pool/src/util.rs`)

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the `min_fee_rate` threshold by computing the minimum required fee using only the transaction's serialized byte size as the weight denominator. However, CKB's canonical weight function `get_transaction_weight` takes the maximum of serialized size and `cycles * DEFAULT_BYTES_PER_CYCLES`. For cycle-heavy transactions the actual weight can be up to ~12× larger than the byte size, meaning the size-only check admits transactions whose true fee rate is far below the configured minimum. Any unprivileged RPC caller can exploit this to flood the tx-pool with transactions whose real fee rate is a fraction of `min_fee_rate`.

### Finding Description

**Root cause — `tx-pool/src/util.rs`, `check_tx_fee`:**

```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(...)
        .transaction_fee(rtx)?;
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

The minimum fee is computed as `min_fee_rate * tx_size / 1000` (shannons). This is the only fee-rate gate at admission time.

**Canonical weight function — `util/types/src/core/tx_pool.rs`:**

```rust
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

With `max_tx_verify_cycles = 70_000_000`, the maximum weight from cycles alone is `70_000_000 × 0.000_170_571_4 ≈ 11,940` virtual bytes.

**Stored fee rate uses actual weight — `tx-pool/src/component/entry.rs`:**

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [3](#0-2) 

The admission check and the stored fee rate use **different weight denominators**. This is the unit mismatch.

**Exploit path:**

1. Attacker crafts a transaction with `tx_size = 1000` bytes and `cycles = 70_000_000` (at the `max_tx_verify_cycles` limit).
2. `check_tx_fee` computes `min_fee = 1000 * 1000 / 1000 = 1000 shannons` and the transaction passes with fee = 1000 shannons.
3. Scripts execute; cycles = 70 M, actual weight = `max(1000, 11940) = 11940`.
4. Stored fee rate = `1000 * 1000 / 11940 ≈ 84 shannons/KW` — roughly **12× below** the `min_fee_rate` of 1000 shannons/KW.
5. The transaction is now resident in the pool with a fee rate that violates the configured minimum.

For smaller transactions (e.g., 200 bytes) the bypass factor reaches ~60×.

### Impact Explanation

An unprivileged attacker can submit transactions that pass the `min_fee_rate` admission gate but carry an actual fee rate far below the threshold. This:

- **Undermines spam protection**: The `min_fee_rate` is the primary economic barrier against tx-pool flooding. Bypassing it allows pool saturation at a fraction of the intended cost.
- **Degrades miner revenue**: Miners selecting transactions by fee rate will include these artificially admitted low-rate transactions, displacing higher-paying ones.
- **Eviction asymmetry**: The pool evicts by actual fee rate (`fee_rate()` using real weight), so these transactions are evicted first when the pool fills — but until then they occupy pool space at below-minimum cost.

### Likelihood Explanation

The entry path is the standard `send_transaction` JSON-RPC endpoint, reachable by any unprivileged caller. Crafting a transaction with near-maximum cycles is straightforward (e.g., a script with a tight computation loop). No privileged access, key material, or majority hashpower is required. The code comment acknowledges the approximation explicitly, confirming the discrepancy is present in production.

### Recommendation

Replace the size-only weight in `check_tx_fee` with the canonical weight function:

```rust
use ckb_types::core::tx_pool::get_transaction_weight;

// After fee is computed and cycles are known (post-script-execution),
// re-check fee rate using actual weight:
let weight = get_transaction_weight(tx_size, cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

Because cycles are only known after script execution, the fee-rate gate should be applied as a post-verification check (after `ContextualTransactionVerifier::verify`) using the returned `Completed { cycles, fee }` values, rather than as a pre-execution cheap check using size alone.

### Proof of Concept

```
Given: min_fee_rate = 1000 shannons/KW, max_tx_verify_cycles = 70_000_000

Craft tx:
  tx_size  = 1000 bytes
  cycles   = 70_000_000
  fee      = 1000 shannons  (exactly the size-based minimum)

check_tx_fee:
  min_fee = fee_rate.fee(1000) = 1000 * 1000 / 1000 = 1000 shannons
  fee (1000) >= min_fee (1000) → PASS

Actual weight = max(1000, 70_000_000 * 0.000_170_571_4) = max(1000, 11940) = 11940
Actual fee rate = 1000 * 1000 / 11940 ≈ 84 shannons/KW

84 << 1000 (min_fee_rate) → transaction admitted at ~12× below minimum fee rate
``` [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```
