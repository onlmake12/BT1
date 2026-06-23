### Title
`check_tx_fee` Minimum Fee Rate Check Uses Only Serialized Size, Ignoring Cycles — Cycle-Heavy Transactions Bypass the Threshold - (File: `tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` enforces the minimum fee rate using only the transaction's serialized byte size as the weight denominator. The actual transaction weight — used everywhere else in the pool for fee-rate ranking and block assembly — is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions the two values diverge by up to ~120×, making the admission gate effectively inoperative for that class of transaction.

---

### Finding Description

CKB defines transaction weight as:

```rust
// util/types/src/core/tx_pool.rs
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [1](#0-0) 

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`, derived from `MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES`. [2](#0-1) 

The minimum fee for a given weight is:

```rust
// util/types/src/core/fee_rate.rs
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;   // KW = 1000
    Capacity::shannons(fee)
}
``` [3](#0-2) 

But the admission gate in `check_tx_fee` passes only `tx_size` — the raw serialized byte count — as the weight, explicitly skipping cycles:

```rust
// tx-pool/src/util.rs
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [4](#0-3) 

This is the **only** fee-rate gate before a transaction enters the pool. There is no second check after `verify_rtx` returns the actual cycle count. The `Completed` result from verification is used to cache the cycle count, but `check_tx_fee` is never re-run with the real weight. [5](#0-4) 

**Concrete arithmetic:**

| Parameter | Value |
|---|---|
| `max_tx_verify_cycles` | 70,000,000 |
| Max weight from cycles | 70,000,000 × 0.000_170_571_4 ≈ **11,940** |
| Minimum plausible `tx_size` | ~100 bytes |
| Ratio (actual weight / size) | ≈ **119×** |

With `min_fee_rate = 1,000 shannons/KW`:

- **Size-based gate** requires: `1,000 × 100 / 1,000 = 100 shannons`
- **Actual weight-based requirement**: `1,000 × 11,940 / 1,000 = 11,940 shannons`

A transaction paying only 100 shannons passes `check_tx_fee` but has an actual fee rate of `100 × 1,000 / 11,940 ≈ 8 shannons/KW` — roughly 125× below the configured threshold.

---

### Impact Explanation

An unprivileged RPC caller or relay peer can submit cycle-heavy, byte-small transactions that pass the minimum fee rate admission check while carrying an actual fee rate far below the configured threshold. Such transactions:

1. Enter the tx-pool legitimately (no rejection).
2. Are sorted near the bottom of the fee-rate queue, so miners deprioritize them.
3. Occupy pool memory until eviction or expiry (default 12 hours).

Repeated submission fills the pool with near-zero-fee-rate entries, degrading pool quality and potentially crowding out legitimate transactions. The pool's 180 MB size cap bounds the absolute damage but does not prevent sustained degradation.

---

### Likelihood Explanation

The attack requires only a valid RPC connection or a peer relay path — no privileged access, no key material, no majority hashpower. Crafting a transaction with high declared cycles and small serialized size is straightforward (e.g., a transaction referencing a large script via cell dep, where the script itself is not serialized in the transaction body). The `max_tx_verify_cycles` limit (70 M) is the only natural bound on the discrepancy.

---

### Recommendation

After `verify_rtx` returns the actual cycle count, re-run the fee rate check using the true weight:

```rust
// After verification, re-check with actual weight
let actual_weight = get_transaction_weight(tx_size, completed.cycles);
let actual_min_fee = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < actual_min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

Alternatively, if the pre-check must remain cheap, document that the post-verification step is the authoritative gate and add the cycle-aware check there.

---

### Proof of Concept

1. Craft a transaction with:
   - Serialized size ≈ 200 bytes (small lock script, one input, one output).
   - A type script that consumes close to `max_tx_verify_cycles` (70 M) cycles.
   - Fee = `min_fee_rate × tx_size / 1000` = `1,000 × 200 / 1,000` = **200 shannons**.

2. Submit via `send_transaction` RPC.

3. `check_tx_fee` computes `min_fee = fee(200) = 200 shannons`. Fee ≥ min_fee → **accepted**.

4. After verification, actual weight ≈ 11,940. Actual fee rate = `200 × 1,000 / 11,940 ≈ 16 shannons/KW` — 62× below the 1,000 shannons/KW threshold.

5. Transaction sits in the pool, ranked near the bottom, for up to 12 hours. Repeat to fill the pool with sub-threshold entries. [5](#0-4) [6](#0-5) [3](#0-2)

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

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
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
