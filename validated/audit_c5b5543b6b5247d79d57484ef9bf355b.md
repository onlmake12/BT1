### Title
Fee Rate Minimum Check Uses Serialized Size Instead of Transaction Weight, Allowing Cycle-Heavy Transactions to Bypass the Minimum Fee Rate — (File: `tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate by computing the required minimum fee using only the transaction's serialized byte size as the weight. However, CKB's canonical transaction weight is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions, the true weight can be an order of magnitude larger than the byte size. Because the only fee-rate gate in the tx-pool admission path uses the smaller byte-size figure, a transaction submitter can craft a cycle-heavy transaction whose fee satisfies the size-based check but is far below the minimum fee rate when measured against the actual weight. This is the direct CKB analog of the Escrow decimal-assumption bug: both share the root cause of a fixed-precision assumption (18 decimals / weight = size) that silently produces a wrong comparison for a well-defined class of inputs.

---

### Finding Description

`FeeRate` is defined as **shannons per kilo-weight**, where weight is:

```
weight = max(tx_size_bytes, cycles × DEFAULT_BYTES_PER_CYCLES)
``` [1](#0-0) 

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` is derived from `MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES`. [2](#0-1) 

The `FeeRate::fee` helper computes the minimum fee for a given weight as `fee_rate × weight / 1000`. [3](#0-2) 

`check_tx_fee`, the sole fee-rate gate in the tx-pool admission path, calls `fee(tx_size as u64)` — substituting raw byte size for weight: [4](#0-3) 

The developer comment acknowledges the mismatch ("Theoretically we cannot use size as weight directly") but treats it as a "cheap check". No subsequent check using the actual weight (which requires the cycle count returned by `verify_rtx`) is present in the admission path. `check_tx_fee` is called in `tx-pool/src/process.rs` (4 call sites) and is the only fee-rate enforcement point.

The fee-rate statistics RPC correctly uses `get_transaction_weight(size, cycles)`: [5](#0-4) 

This creates a split: the admission gate uses size; the statistics path uses weight. A transaction admitted via the size-based gate can have an effective fee rate far below `min_fee_rate` when measured by the weight-based formula.

---

### Impact Explanation

**Concrete example** with default `min_fee_rate = 1000` shannons/KW:

| Parameter | Value |
|---|---|
| `tx_size` | 1 000 bytes |
| `cycles` | 70 000 000 (protocol max per tx) |
| `weight` | `max(1000, 70 000 000 × 0.000 170 571 4)` = **11 940** |
| Fee required by `check_tx_fee` | `1000 × 1000 / 1000` = **1 000 shannons** |
| Fee required at true weight | `1000 × 11 940 / 1000` = **11 940 shannons** |

An attacker pays **1 000 shannons** for a transaction that consumes the equivalent of **11 940 bytes** of block capacity. The effective fee rate is `1000 × 1000 / 11940 ≈ 83.8` shannons/KW — **11.9× below the configured minimum**.

Impact: an unprivileged tx-pool submitter can flood the mempool with cycle-heavy, underpriced transactions at a fraction of the intended cost, degrading node performance and displacing legitimately priced transactions.

---

### Likelihood Explanation

- The attack requires only a valid CKB transaction with a script that consumes many cycles but has a small serialized body — a normal property of any non-trivial lock or type script.
- No privileged access, key material, or majority hashpower is needed.
- The attacker controls both the script (cycle count) and the fee (capacity delta), making the exploit fully parameterizable.
- The default `max_tx_verify_cycles = 70_000_000` sets a hard upper bound on cycles, so the maximum weight amplification factor is bounded but still ~12×.

---

### Recommendation

After `verify_rtx` returns the actual cycle count, perform a second fee-rate check using the true weight:

```rust
let weight = get_transaction_weight(tx_size, completed.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

Alternatively, apply a conservative upper-bound estimate of cycles before script execution (e.g., using the declared cycle limit) so that a single check suffices.

---

### Proof of Concept

1. Craft a CKB transaction whose lock script runs a tight loop consuming ~70 000 000 cycles. Keep the serialized transaction body small (e.g., 1 000 bytes).
2. Set the fee to exactly `min_fee_rate × tx_size / 1000 = 1 000 × 1 000 / 1 000 = 1 000` shannons.
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = FeeRate(1000).fee(1000) = 1000 shannons`; the fee equals the threshold, so the transaction is accepted.
5. The actual weight is `max(1000, 70 000 000 × 0.000_170_571_4) = 11 940`; the effective fee rate is `1000 × 1000 / 11940 ≈ 84` shannons/KW — well below the 1 000 shannons/KW minimum.
6. Repeat to fill the mempool with cycle-heavy, underpriced transactions. [6](#0-5) [7](#0-6) [8](#0-7)

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

**File:** util/types/src/core/fee_rate.rs (L11-16)
```rust
    pub fn calculate(fee: Capacity, weight: u64) -> Self {
        if weight == 0 {
            return FeeRate::zero();
        }
        FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
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
