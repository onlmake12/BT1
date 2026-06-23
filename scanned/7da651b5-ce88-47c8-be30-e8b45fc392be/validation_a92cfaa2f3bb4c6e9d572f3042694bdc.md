### Title
`check_tx_fee` Uses Serialized Byte Size Instead of Weight Units for Minimum Fee Enforcement, Allowing Compute-Heavy Transactions to Bypass Fee Gate - (File: `tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee by passing raw serialized byte size (`tx_size`) directly to `FeeRate::fee()`, which expects a **weight** value (in weight units). For compute-heavy transactions, the true weight can be up to ~60× larger than the byte size, meaning the minimum fee gate is proportionally weaker. An unprivileged RPC caller can submit transactions that pass the fee check while paying far less than `min_fee_rate` actually requires, forcing the node to execute expensive CKB-VM scripts at a fraction of the intended cost.

---

### Finding Description

`FeeRate` is defined as **shannons per kilo-weight** (shannons/KW), where weight is:

```
weight = max(tx_size_bytes, cycles × DEFAULT_BYTES_PER_CYCLES)
``` [1](#0-0) [2](#0-1) 

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`, so a transaction consuming the maximum `max_tx_verify_cycles = 70,000,000` cycles has a weight of approximately `70,000,000 × 0.000_170_571_4 ≈ 11,940` weight units.

In `check_tx_fee`, however, the minimum fee is computed by substituting `tx_size` (bytes) for weight:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [3](#0-2) 

`FeeRate::fee(weight)` computes `fee_rate × weight / 1000`. By passing `tx_size` (bytes) instead of the true weight, the code implicitly assumes a 1:1 exchange rate between bytes and weight units — exactly the same class of unit mismatch as the reported LP token/dollar bug.

For a compute-heavy transaction with, say, 200 serialized bytes and 70 M cycles:

| Quantity | Value |
|---|---|
| `tx_size` (bytes) | 200 |
| True weight | ≈ 11,940 |
| `min_fee` charged by gate | `1000 × 200 / 1000 = 200 shannons` |
| Fee actually required at `min_fee_rate` | `1000 × 11,940 / 1000 = 11,940 shannons` |
| Discount factor | **~60×** |

The actual fee rate stored in the pool entry is computed correctly via `get_transaction_weight`: [4](#0-3) [5](#0-4) 

So the pool's internal fee-rate ordering and eviction are correct — only the **admission gate** is wrong.

---

### Impact Explanation

An unprivileged tx-pool submitter (RPC caller via `send_transaction`) can craft a transaction that:

1. Has a small serialized size (e.g., 200 bytes) but consumes up to `max_tx_verify_cycles = 70,000,000` cycles.
2. Pays only `min_fee_rate × tx_size / 1000` shannons — up to ~60× less than `min_fee_rate` actually requires.
3. Passes `check_tx_fee` and proceeds to full CKB-VM script verification.
4. Forces the node to spend up to 70 M cycles of script execution per transaction at a fraction of the intended fee cost.

By submitting many such transactions in parallel, an attacker can saturate the node's script verification workers with near-zero economic cost, constituting a **CPU-exhaustion DoS** against the tx-pool verification pipeline. The `max_tx_pool_size` and eviction logic do not prevent this because the damage occurs during the verification phase, before eviction can act. [6](#0-5) 

---

### Likelihood Explanation

- The attack requires only a valid RPC connection to a CKB node — no privileged access, no key material, no majority hashpower.
- Crafting a compute-heavy transaction (e.g., a script that loops for 70 M cycles) is straightforward for any script author.
- The fee cost to the attacker is ~60× lower than the node operator intends, making sustained attacks economically viable.
- The node's default `max_tx_verify_cycles = 70,000,000` and `max_tx_verify_workers` (3/4 of CPU cores) amplify the impact per transaction.

---

### Recommendation

Replace the `tx_size`-based cheap check with the correct weight-based check:

```rust
let weight = get_transaction_weight(tx_size, /* cycles from cache or estimate */);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

If cycles are not yet known at the pre-verification stage, use `tx_size` as a **lower bound** but also enforce a separate per-byte minimum, or defer the fee-rate check until after verification completes and the true cycle count is known. At minimum, document the known gap and add a post-verification fee-rate re-check that rejects the transaction if its true fee rate (computed with `get_transaction_weight`) falls below `min_fee_rate`.

---

### Proof of Concept

1. Construct a CKB transaction whose lock or type script loops for ~70,000,000 cycles but whose serialized size is ~200 bytes.
2. Set the transaction fee to `ceil(min_fee_rate × 200 / 1000)` shannons (e.g., 200 shannons at the default `min_fee_rate = 1000`).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200 shannons` → **passes**.
5. The node proceeds to full CKB-VM verification, executing 70 M cycles.
6. The transaction is admitted to the pool with an actual fee rate of `1000 × 200 / 11940 ≈ 16 shannons/KW` — far below `min_fee_rate = 1000`.
7. Repeat with many transactions to exhaust the verification worker pool.

Relevant code path: [7](#0-6) [8](#0-7) [2](#0-1)

### Citations

**File:** util/types/src/core/tx_pool.rs (L279-303)
```rust
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

**File:** util/app-config/src/legacy/tx_pool.rs (L9-12)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
```
