### Title
`FeeRate::fee` Rounds DOWN When Computing Minimum Required Fee, Allowing Transactions Below `min_fee_rate` Into the Tx-Pool — (`util/types/src/core/fee_rate.rs`)

### Summary

`FeeRate::fee` uses integer floor division to compute the minimum fee required for a given transaction weight. When this result is used as the threshold in `check_tx_fee`, a transaction whose true fee rate is fractionally below `min_fee_rate` can satisfy the check and be admitted to the tx-pool. The correct rounding direction for a minimum-fee gate is ceiling (round up), not floor (round down).

### Finding Description

`FeeRate::fee` is defined as:

```rust
// util/types/src/core/fee_rate.rs
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;   // KW = 1000, plain floor division
    Capacity::shannons(fee)
}
``` [1](#0-0) 

This is the sole implementation used to derive `min_fee` in the tx-pool admission gate:

```rust
// tx-pool/src/util.rs
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [2](#0-1) 

Because `fee_rate * weight / 1000` truncates, `min_fee` is the floor of the exact threshold. A transaction whose actual fee rate is `min_fee_rate - ε` (for small ε) can produce a fee equal to `floor(min_fee_rate * weight / 1000)`, which is identical to `min_fee`, and therefore passes the `fee < min_fee` check.

**Concrete example**

| Parameter | Value |
|---|---|
| `min_fee_rate` | 1000 shannons/KW |
| `tx_size` | 1001 bytes |
| `min_fee` (floor) | `floor(1000 × 1001 / 1000)` = **1001** shannons |
| Attacker submits fee | **1001** shannons |
| Attacker's actual fee rate | `1001 × 1000 / 1001` ≈ **999.999** shannons/KW |
| Check `fee < min_fee` | `1001 < 1001` → **false → admitted** |

The transaction is admitted despite its fee rate being below the configured minimum.

The same `FeeRate::fee` path is also invoked inside `calculate_min_replace_fee` for RBF replacement checks, so the same rounding error applies there as well. [3](#0-2) 

The symmetric function `FeeRate::calculate` also uses floor division:

```rust
FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
``` [4](#0-3) 

This means a transaction's stored fee rate is always rounded down, compounding the admission error when fee rates are compared for eviction or RBF ordering.

### Impact Explanation

An unprivileged tx-pool submitter can craft transactions whose true fee rate is up to `(KW-1)/weight` shannons/KW below `min_fee_rate` and have them accepted. For typical transaction sizes (~1 KB), the bypass is at most ~1 shannon/KW per transaction. While small per transaction, this systematically undermines the minimum-fee-rate spam-prevention policy: an attacker can fill the mempool with transactions that are technically below the configured floor, degrading the fee market signal and potentially displacing higher-fee-rate transactions from block templates. The same bypass applies to RBF minimum-fee enforcement, allowing replacement transactions to be accepted with a fee increment slightly below the required minimum.

### Likelihood Explanation

Any tx-pool submitter (no privilege required) can trigger this by choosing a transaction size that is not a multiple of 1000 bytes, which is the common case for real transactions. The attacker needs no special knowledge beyond the node's public `min_fee_rate` configuration.

### Recommendation

Replace the floor division in `FeeRate::fee` with ceiling division when the result is used as a minimum-fee gate:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    // Use div_ceil so the minimum fee always meets or exceeds the configured rate.
    let fee = self.0.saturating_mul(weight).div_ceil(KW);
    Capacity::shannons(fee)
}
```

This mirrors the pattern already used in `transferred_byte_cycles`:

```rust
// script/src/cost_model.rs
pub fn transferred_byte_cycles(bytes: u64) -> u64 {
    bytes.div_ceil(BYTES_PER_CYCLE)   // ceiling, not floor
}
``` [5](#0-4) 

If `FeeRate::fee` is also used in contexts where floor rounding is correct (e.g., computing an actual fee to pay rather than a minimum threshold), introduce a separate `min_fee` method with ceiling semantics, analogous to the Centrifuge fix of adding a `Math.Rounding` argument.

### Proof of Concept

1. Node configured with `min_fee_rate = 1000` shannons/KW.
2. Attacker constructs a transaction of serialized size **1001 bytes**.
3. `min_fee = FeeRate(1000).fee(1001) = floor(1000 × 1001 / 1000) = 1001` shannons.
4. Attacker sets output capacity such that `inputs_capacity - outputs_capacity = 1001` shannons.
5. `check_tx_fee`: `1001 < 1001` is false → transaction admitted.
6. Actual fee rate: `1001 × 1000 / 1001 ≈ 999.999` shannons/KW — below the 1000 shannons/KW minimum.
7. Repeat with varying sizes (any `tx_size` not divisible by 1000) to continuously inject below-minimum-fee-rate transactions. [6](#0-5) [1](#0-0)

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

**File:** tx-pool/src/pool.rs (L664-671)
```rust
        let fee = entry.fee;
        if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
            if fee < min_replace_fee {
                return Err(Reject::RBFRejected(format!(
                    "Tx's current fee is {}, expect it to >= {} to replace old txs",
                    fee, min_replace_fee,
                )));
            }
```

**File:** script/src/cost_model.rs (L10-13)
```rust
pub fn transferred_byte_cycles(bytes: u64) -> u64 {
    // Compiler will optimize the divisin here to shifts.
    bytes.div_ceil(BYTES_PER_CYCLE)
}
```
