### Title
Tx-pool minimum-fee admission check uses serialized `size` instead of `weight`, allowing high-cycle transactions to undercut the fee-rate guard — (File: tx-pool/src/util.rs)

### Summary
The tx-pool admission function `check_tx_fee` computes the minimum required fee using the transaction's raw serialized size, while every other fee-rate calculation in the pool (scoring, eviction, RBF) uses `weight = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For a transaction whose cycle count dominates, `weight >> size`, so the admission check is materially weaker than the pool's own eviction criterion. A tx-pool submitter can craft a transaction with high cycles but small serialized size and be admitted at a fee that is far below the effective minimum fee rate.

### Finding Description

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum fee as:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The code comment immediately above this line explicitly acknowledges the inconsistency:

> "Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly"

Meanwhile, every other fee-rate computation in the pool uses `get_transaction_weight`:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

`TxEntry::fee_rate()` (used for scoring and eviction) calls `get_transaction_weight`:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

<cite repo="Mortal4ever/ckb--012" path="

### Citations

**File:** tx-pool/src/util.rs (L42-52)
```rust
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
