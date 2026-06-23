### Title
Wrong Rounding Direction in `FeeRate::fee()` Allows Transactions to Slightly Underpay Minimum Fee Rate - (File: `util/types/src/core/fee_rate.rs`)

### Summary

`FeeRate::fee()` computes the minimum fee threshold for a transaction using integer floor division. Because it rounds **down** instead of **up**, the computed minimum fee is strictly less than the true mathematical minimum, allowing a transaction sender to pay slightly less than the configured `min_fee_rate` and still pass the pool admission check.

### Finding Description

`FeeRate::fee()` computes the minimum fee for a given transaction weight as:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;   // KW = 1000
    Capacity::shannons(fee)
}
``` [1](#0-0) 

The result is truncated (floor division). This value is then used as the rejection threshold in `check_tx_fee`:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [2](#0-1) 

Because `min_fee` is rounded **down**, a transaction whose actual fee equals the truncated value passes the check even though its effective fee rate is below `min_fee_rate`.

**Concrete example:**
- `min_fee_rate = 1001 shannons/KW`, `tx_size = 999 bytes`
- `min_fee = 1001 * 999 / 1000 = 999999 / 1000 = 999` (floor)
- True minimum = ⌈999.999⌉ = 1000 shannons
- A transaction paying 999 shannons passes, with an effective fee rate of `999 * 1000 / 999 ≈ 1000 shannons/KW` — below the configured minimum of 1001.

The correct implementation should use ceiling division:
```rust
let fee = (self.0.saturating_mul(weight) + KW - 1) / KW;
```

### Impact Explanation

**Impact: Low.** The maximum underpayment per transaction is strictly less than 1 shannon (the smallest CKB unit), bounded by `(fee_rate * weight % KW) / KW < 1`. This does not allow a transaction to completely bypass the fee check, but it does technically violate the minimum fee rate guarantee. Transactions that underpay by this margin are accepted into the pool and may be relayed to peers, slightly undermining the spam-prevention intent of `min_fee_rate`.

### Likelihood Explanation

**Likelihood: High.** The division in `FeeRate::fee()` is executed on every transaction submitted to the pool via `send_transaction` or `send_test_transaction` RPC, and on every relayed transaction. Any transaction sender whose `tx_size` is not a multiple of `KW` (i.e., almost every real transaction) can trigger this rounding shortfall. [3](#0-2) 

### Recommendation

Change `FeeRate::fee()` to use ceiling division so that the computed minimum fee is always at least the true mathematical minimum, favoring the protocol over the transaction sender:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    let fee = (self.0.saturating_mul(weight) + KW - 1) / KW;
    Capacity::shannons(fee)
}
``` [1](#0-0) 

### Proof of Concept

1. Node configured with `min_fee_rate = 1001 shannons/KW` (default is 1000; any non-round value works).
2. Attacker crafts a transaction of serialized size `tx_size = 999` bytes.
3. True minimum fee = ⌈1001 × 999 / 1000⌉ = ⌈999.999⌉ = **1000 shannons**.
4. `FeeRate::fee(999)` returns `1001 * 999 / 1000 = 999` shannons (floor).
5. Attacker sets output capacity so that `fee = 999 shannons`.
6. `check_tx_fee` evaluates `999 < 999` → **false** → transaction is **accepted**.
7. The transaction's effective fee rate is `999 * 1000 / 999 ≈ 1000 shannons/KW`, which is below the configured minimum of 1001 shannons/KW. [4](#0-3) [5](#0-4)

### Citations

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
