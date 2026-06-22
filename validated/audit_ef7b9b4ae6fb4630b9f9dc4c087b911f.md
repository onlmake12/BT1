### Title
Fee Rate Unit Mismatch in `check_tx_fee` Allows High-Cycle Transactions to Bypass Minimum Fee Rate Policy - (File: tx-pool/src/util.rs)

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` computes the minimum required fee using raw serialized transaction size (`tx_size`) as the weight argument to `FeeRate::fee()`. However, `FeeRate` is defined as **shannons per kilo-weight**, where weight is `get_transaction_weight(size, cycles) = max(size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For transactions where cycles dominate, the actual weight is significantly larger than `tx_size`, so the minimum fee check is far too lenient. An unprivileged tx-pool submitter can craft a valid transaction with high cycles and small serialized size to bypass the node operator's `min_fee_rate` policy by a factor proportional to `weight / tx_size`.

### Finding Description

`FeeRate` is defined as shannons per kilo-weight (KW = 1000):

```rust
// util/types/src/core/fee_rate.rs
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;
    Capacity::shannons(fee)
}
``` [1](#0-0) [2](#0-1) 

The transaction weight is defined as:

```rust
// util/types/src/core/tx_pool.rs
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [3](#0-2) 

But `check_tx_fee` passes raw `tx_size` (bytes) — not the actual weight — to `FeeRate::fee()`:

```rust
// tx-pool/src/util.rs
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [4](#0-3) 

The code comment acknowledges the approximation ("here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly"), but this is the **only** fee rate check in the admission path. There is no second weight-based check after actual cycles are determined by script execution.

### Impact Explanation

**Concrete scenario with default config (`min_fee_rate = 1000` shannons/KW, `max_tx_verify_cycles = 70,000,000`):**

- Attacker submits a transaction with a complex lock script consuming ~70,000,000 cycles but with a small serialized size of ~242 bytes (a typical 2-in-2-out transaction skeleton).
- Actual weight = `max(242, 70_000_000 × 0.000_170_571_4)` ≈ `max(242, 11,940)` = **11,940 bytes**.
- `check_tx_fee` computes: `min_fee = 1000 × 242 / 1000 = 242 shannons`.
- Correct minimum fee should be: `1000 × 11,940 / 1000 = 11,940 shannons`.
- **Attacker pays ~49× less than the effective minimum fee rate.** [5](#0-4) 

The node operator's `min_fee_rate` policy — intended to prevent spam and ensure economic cost for consuming node resources — is bypassed for any transaction where cycles dominate the weight. This enables a resource exhaustion attack: an attacker can flood the tx-pool with high-cycle, small-size transactions at a fraction of the intended cost, consuming CPU cycles during script verification and pool memory.

**Impact:** High — fee rate policy bypass enabling cheap resource exhaustion of tx-pool CPU and memory.
**Likelihood:** Medium — requires crafting a valid transaction with high cycles and small size, which is achievable by any script author.

### Likelihood Explanation

Any unprivileged RPC caller with access to `send_transaction` can submit transactions. Crafting a valid transaction with high cycles is straightforward: a lock script that performs many iterations (e.g., a loop-heavy RISC-V program) can consume up to `max_tx_verify_cycles = 70,000,000` cycles while having a small serialized size. No privileged access, leaked keys, or majority hashpower is required. [6](#0-5) 

### Recommendation

Replace the size-only weight approximation in `check_tx_fee` with the actual transaction weight. Since cycles are not yet known at the time of the cheap check, use the declared cycles (from the transaction's `witnesses` or a pre-declared cycle limit) or apply a conservative upper bound. After script execution determines actual cycles, perform a second fee rate check using `get_transaction_weight(tx_size, actual_cycles)`:

```rust
// After verify_rtx returns actual cycles:
let weight = get_transaction_weight(tx_size, actual_cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [5](#0-4) 

### Proof of Concept

1. Craft a CKB transaction with a lock script that executes a tight loop consuming ~70,000,000 cycles. The serialized transaction size is ~300 bytes.
2. Compute the fee: `min_fee_rate (1000) × 300 / 1000 = 300 shannons`. Set the transaction fee to exactly 300 shannons.
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` passes: `300 >= 1000 × 300 / 1000 = 300`. ✓
5. `verify_rtx` executes the script, consuming 70,000,000 cycles.
6. Actual weight = `max(300, 11,940)` = 11,940. Effective fee rate = `300 × 1000 / 11,940 ≈ 25 shannons/KW` — far below the configured 1,000 shannons/KW minimum.
7. The transaction is admitted to the pool. Repeat to flood the pool with high-cycle transactions at ~49× below the intended minimum cost. [7](#0-6) [4](#0-3)

### Citations

**File:** util/types/src/core/fee_rate.rs (L3-7)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
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
