### Title
Unscaled Weight in Tx-Pool Minimum Fee Rate Check Allows Cycle-Heavy Transactions to Bypass `min_fee_rate` — (`File: tx-pool/src/util.rs`)

---

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` computes the minimum required fee using only the serialized byte size of a transaction, ignoring the cycles dimension. The correct normalized weight — `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)` — is used everywhere else (sorting, eviction, fee estimation), but not at the admission gate. An unprivileged submitter can craft a transaction with a small serialized size but near-maximum cycles, pay a fee that satisfies the size-only check, and be admitted to the pool with an effective fee rate up to ~60× below `min_fee_rate`.

---

### Finding Description

CKB transactions consume two independent block resources: serialized byte size (capped at `MAX_BLOCK_BYTES`) and VM cycles (capped at `MAX_BLOCK_CYCLES`). To unify these into a single comparable metric, CKB defines a "weight":

```rust
// util/types/src/core/tx_pool.rs
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [1](#0-0) 

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` converts cycles into a byte-equivalent, so both dimensions are normalized before comparison. [2](#0-1) 

This weight is used correctly in `TxEntry::fee_rate()`, `AncestorsScoreSortKey`, `EvictKey`, and `FeeRateCollector::statistics`. However, the admission gate `check_tx_fee` uses only `tx_size`:

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

`FeeRate::fee(weight)` computes `fee_rate * weight / 1000`. When `weight = tx_size` instead of `get_transaction_weight(tx_size, cycles)`, the required fee is proportional only to bytes, not to the actual resource consumption. [4](#0-3) 

---

### Impact Explanation

**Concrete numbers:**

| Parameter | Value |
|---|---|
| `max_tx_verify_cycles` | 70,000,000 |
| `DEFAULT_BYTES_PER_CYCLES` | 0.000_170_571_4 |
| Cycle-equivalent bytes at max cycles | `70,000,000 × 0.000_170_571_4 ≈ 11,940` |
| Minimum realistic tx size | ~200 bytes |
| Maximum weight ratio | `11,940 / 200 ≈ 60×` |

An attacker submits a transaction with:
- Serialized size: 200 bytes
- Cycles: 70,000,000
- Fee: 201 shannons (just above `min_fee_rate(1000) × 200 / 1000 = 200 shannons`)

`check_tx_fee` passes (201 > 200). The transaction is admitted.

Actual weight: `max(200, 11,940) = 11,940`. Actual effective fee rate: `201 × 1000 / 11,940 ≈ 16.8 shannons/KW` — far below the configured `min_fee_rate` of 1,000 shannons/KW.

The attacker fills the mempool at ~1/60th the intended cost. These transactions are also relayed to peers (which apply the same flawed check), propagating the attack across the network.

---

### Likelihood Explanation

- Entry path is fully open: any caller of the `send_transaction` RPC or P2P relay path reaches `check_tx_fee`.
- No special privilege, key, or majority hashpower is required.
- The discrepancy is deterministic and reproducible.
- The code comment acknowledges the theoretical issue, confirming the gap is real and not a misread. [5](#0-4) 

---

### Recommendation

Replace the size-only weight in `check_tx_fee` with the normalized weight:

```rust
// tx-pool/src/util.rs
// Need cycles to compute proper weight; pass cycles alongside tx_size
let weight = get_transaction_weight(tx_size, cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

The `check_tx_fee` signature should accept `cycles: Cycle` in addition to `tx_size: usize`, consistent with how `TxEntry::fee_rate()` and all other fee-rate computations work. [6](#0-5) 

---

### Proof of Concept

**Attacker constructs a transaction:**
- Serialized size: 200 bytes (small lock script, minimal inputs/outputs)
- Script that consumes ~70,000,000 cycles (e.g., a tight loop in CKB-VM)
- Fee: 201 shannons

**At admission (`check_tx_fee`):**
```
min_fee = min_fee_rate.fee(tx_size) = 1000 * 200 / 1000 = 200 shannons
fee (201) >= min_fee (200) → ADMITTED
```

**Actual effective fee rate:**
```
weight = max(200, 70_000_000 * 0.000_170_571_4) = max(200, 11_940) = 11_940
effective_fee_rate = 201 * 1000 / 11_940 ≈ 16.8 shannons/KW
```

This is 98.3% below the configured `min_fee_rate` of 1,000 shannons/KW. The attacker can submit thousands of such transactions, flooding the mempool and relaying them to peers at a cost ~60× lower than the protocol intends to require. [7](#0-6) [2](#0-1)

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

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
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
