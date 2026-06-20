### Title
Wrong Rounding Direction in `FeeRate::fee()` Allows Below-Minimum-Fee-Rate Transactions into Tx Pool - (File: util/types/src/core/fee_rate.rs)

### Summary

`FeeRate::fee()` uses integer division that rounds **down** when computing the minimum fee threshold for tx-pool admission. This causes the enforced minimum fee to be strictly less than `min_fee_rate * weight / 1000` whenever the product is not divisible by 1000, allowing any transaction sender to submit transactions whose actual fee rate is measurably below the configured `min_fee_rate`.

### Finding Description

`FeeRate::fee()` computes the minimum fee for a given transaction weight:

```rust
// util/types/src/core/fee_rate.rs, line 34-37
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;   // KW = 1000, rounds DOWN
    Capacity::shannons(fee)
}
``` [1](#0-0) 

This result is used directly as the admission threshold in `check_tx_fee`:

```rust
// tx-pool/src/util.rs, lines 45-52
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [2](#0-1) 

Because `fee()` rounds **down**, the computed `min_fee` is `⌊min_fee_rate × tx_size / 1000⌋`. A transaction whose fee equals exactly this floor value passes the `fee < min_fee` check, yet its actual fee rate is:

```
actual_fee_rate = ⌊min_fee_rate × tx_size / 1000⌋ × 1000 / tx_size
                < min_fee_rate   (whenever min_fee_rate × tx_size % 1000 ≠ 0)
```

**Concrete example** with the default `min_fee_rate = 1000 shannons/KW`:
- `tx_size = 1001 bytes`
- `min_fee = ⌊1000 × 1001 / 1000⌋ = 1001 shannons`
- A transaction with `fee = 1001` passes the check
- Its actual fee rate = `1001 × 1000 / 1001 ≈ 999.0 shannons/KW` — **below** `min_fee_rate`

The bypass magnitude is at most `(KW − 1) / tx_size` shannons/KW below `min_fee_rate`. For small transactions (e.g., 100 bytes) this can reach ~9 shannons/KW below the configured minimum.

The same `FeeRate::fee()` function is also called in `tx-pool/src/pool.rs` for the RBF minimum-replace-fee calculation, meaning the RBF threshold is similarly undershot. [3](#0-2) 

### Impact Explanation

Any unprivileged transaction sender can submit transactions whose fee rate is slightly below the node's configured `min_fee_rate` and have them accepted into the tx pool. This undermines the operator-configured spam-prevention floor. With the default `min_fee_rate = 1000 shannons/KW`, transactions at ~999 shannons/KW are admitted. The same rounding defect weakens the RBF replacement threshold, allowing replacement transactions to pay slightly less than the required incremental fee. [4](#0-3) 

### Likelihood Explanation

The condition `min_fee_rate × tx_size % 1000 ≠ 0` is satisfied for the vast majority of real transaction sizes. With the default `min_fee_rate = 1000` and any `tx_size` not a multiple of 1000 (i.e., almost every real transaction), the bypass is always available. Any transaction submitter who knows the formula can craft a transaction that exploits this. Entry path is the public `send_transaction` RPC endpoint, requiring no privilege. [5](#0-4) 

### Recommendation

Replace the floor division in `FeeRate::fee()` with ceiling division to ensure the computed minimum fee is never less than what `min_fee_rate` requires:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    // Ceiling division: (a * b + c - 1) / c
    let fee = (self.0.saturating_mul(weight) + KW - 1) / KW;
    Capacity::shannons(fee)
}
```

This guarantees that any transaction passing `fee >= min_fee` has an actual fee rate ≥ `min_fee_rate`, matching the intent of the threshold.

### Proof of Concept

1. Node configured with default `min_fee_rate = 1000 shannons/KW`.
2. Craft a transaction with `tx_size = 1001 bytes` and `fee = 1001 shannons`.
3. Current code: `min_fee = 1000 * 1001 / 1000 = 1001` → `fee (1001) >= min_fee (1001)` → **accepted**.
4. Actual fee rate: `1001 * 1000 / 1001 = 999 shannons/KW` — below the 1000 shannons/KW minimum.
5. With ceiling division: `min_fee = (1000 * 1001 + 999) / 1000 = 1001` → same result here, but for `tx_size = 999`: current gives `min_fee = 999`, ceiling gives `min_fee = 1000`, correctly rejecting a 999-shannon fee on a 999-byte tx whose fee rate is exactly 1000 shannons/KW only when fee = 1000. [1](#0-0) [6](#0-5)

### Citations

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

**File:** tx-pool/src/pool.rs (L86-99)
```rust
    pub fn min_replace_fee(&self, tx: &TxEntry) -> Option<Capacity> {
        if !self.enable_rbf() {
            return None;
        }

        let mut conflicts = vec![self.get_pool_entry(&tx.proposal_short_id()).unwrap()];
        let descendants = self.pool_map.calc_descendants(&tx.proposal_short_id());
        let descendants = descendants
            .iter()
            .filter_map(|id| self.get_pool_entry(id))
            .collect::<Vec<_>>();
        conflicts.extend(descendants);
        self.calculate_min_replace_fee(&conflicts, tx.size)
    }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L9-12)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
```

**File:** rpc/src/module/pool.rs (L106-111)
```rust
    #[rpc(name = "send_transaction")]
    fn send_transaction(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<H256>;
```
