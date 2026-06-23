### Title
Minimum Fee Rate Protection Bypassed via Incorrect Weight in `check_tx_fee` — (`tx-pool/src/util.rs`)

---

### Summary

The tx-pool admission function `check_tx_fee` enforces the minimum fee rate using only the transaction's serialized byte size (`tx_size`) rather than the actual transaction weight (`max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`). For compute-heavy transactions where cycles dominate, `weight > tx_size`, so the required minimum fee is computed against a smaller denominator than the one used for actual fee-rate accounting. An unprivileged submitter can craft a transaction that passes the admission check while its true fee rate is materially below `min_fee_rate`.

---

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` computes the minimum required fee as:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

The actual fee rate of a `TxEntry` is computed using `get_transaction_weight`, which is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [2](#0-1) 

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [3](#0-2) 

When cycles dominate (`cycles * DEFAULT_BYTES_PER_CYCLES > tx_size`), `weight > tx_size`. The admission check uses `tx_size` (the smaller value) to compute `min_fee`, while the true fee rate is computed against `weight` (the larger value). This is structurally identical to the external report: a protection threshold is computed against the user's partial/smaller value instead of the actual full value.

`check_tx_fee` is the only fee-rate admission gate in the tx-pool submission path. [4](#0-3) 

---

### Impact Explanation

An attacker can submit transactions with small serialized size but high cycle consumption (e.g., a script with a tight compute loop). Such a transaction pays a fee just above `min_fee_rate * tx_size / 1000` shannons, which passes `check_tx_fee`, but its true fee rate — computed as `fee * 1000 / weight` — is below `min_fee_rate`. The tx-pool's economic spam-protection threshold is silently bypassed. The attacker can fill the pool with below-minimum-fee-rate transactions, displacing legitimate transactions and degrading node performance.

---

### Likelihood Explanation

The entry path is the standard `send_transaction` RPC, reachable by any unprivileged peer or local caller. No special role, key, or majority hashpower is required. The attacker only needs to deploy a compute-heavy lock or type script (which is permissionless on CKB) and submit transactions spending cells locked by it. The bypass is deterministic and repeatable.

---

### Recommendation

Replace `tx_size` with the actual transaction weight in the admission check:

```rust
let weight = get_transaction_weight(tx_size, /* declared cycles */);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

The declared cycles are available at the point `check_tx_fee` is called (they are passed through the submission pipeline). Using `weight` aligns the admission threshold with the fee-rate metric used for sorting and eviction, closing the gap between the "cheap check" and the true fee rate.

---

### Proof of Concept

**Parameters:**
- `min_fee_rate` = 1,000 shannons/kW (1 shannon per weight unit)
- Transaction serialized size = 100 bytes
- Transaction cycles = 1,000,000

**Weight calculation:**
```
weight = max(100, 1_000_000 * 0.000_170_571_4) = max(100, 170) = 170
```

**Admission check (uses tx_size = 100):**
```
min_fee = 1000 * 100 / 1000 = 100 shannons
```
Attacker pays exactly 100 shannons → **passes** `check_tx_fee`.

**True fee rate (uses weight = 170):**
```
actual_fee_rate = 100 * 1000 / 170 ≈ 588 shannons/kW
```

The transaction enters the pool with an actual fee rate of ~588 shannons/kW, which is **41% below** the configured `min_fee_rate` of 1,000 shannons/kW. By scaling cycles further (e.g., 10,000,000 cycles → weight = 1,705), the attacker can reduce the effective fee rate to ~59 shannons/kW while still passing the admission check.

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

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```
