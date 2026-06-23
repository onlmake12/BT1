### Title
`FeeRate::fee()` Integer Division Rounds Down to Zero, Allowing Zero-Fee Transactions to Bypass `min_fee_rate` Admission Check — (File: `util/types/src/core/fee_rate.rs`, `tx-pool/src/util.rs`)

---

### Summary

`FeeRate::fee()` computes the minimum required fee using integer (floor) division. When `min_fee_rate × tx_size < 1000`, the result truncates to zero. In `check_tx_fee`, the guard `if fee < min_fee` then becomes `if fee < 0`, which is always `false` for the unsigned `Capacity` type, so any transaction — including a zero-fee one — passes the admission check. This is the direct CKB analog of the external report's `round down → zero → incorrect boolean → wrong control flow` pattern.

---

### Finding Description

**Root cause — `FeeRate::fee()`** [1](#0-0) 

```rust
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;   // KW = 1000, integer division
    Capacity::shannons(fee)
}
```

`KW = 1000` is the denominator. Whenever `self.0 (min_fee_rate) × weight (tx_size) < 1000`, the division truncates to **0**.

**Incorrect control flow — `check_tx_fee`** [2](#0-1) 

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
// reject txs which fee lower than min fee rate
if fee < min_fee {          // if min_fee == 0, this is always false
    return Err(reject);
}
```

`fee` is `Capacity` (a `u64` wrapper), so `fee < 0` is structurally impossible. The rejection branch is never taken, and a zero-fee transaction is admitted.

**Concrete trigger condition**

| `min_fee_rate` | `tx_size` | `min_fee` computed | Effect |
|---|---|---|---|
| 1000 (default) | 200 bytes | 200 shannons | Correct |
| 4 | 200 bytes | `4×200/1000 = 0` | **Zero-fee tx admitted** |
| 1 | 999 bytes | `1×999/1000 = 0` | **Zero-fee tx admitted** |

The same truncation affects `calculate_min_replace_fee` for RBF: [3](#0-2) 

```rust
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```

When `min_rbf_rate × size < 1000`, `extra_rbf_fee = 0`, so the RBF replacement requires no incremental fee beyond the replaced transactions' fees, enabling costless RBF cycling.

---

### Impact Explanation

Any transaction sender (unprivileged RPC caller via `send_transaction`) can submit zero-fee transactions that bypass the `min_fee_rate` guard and enter the tx pool. This enables:

1. **Tx-pool spam / resource exhaustion** — the pool fills with zero-fee transactions that miners have no incentive to include, blocking legitimate transactions.
2. **Costless RBF cycling** — when `min_rbf_rate` also rounds to zero, an attacker can repeatedly replace transactions at no cost, wasting node CPU and bandwidth.

---

### Likelihood Explanation

The default `min_fee_rate = 1000` shannons/KW means `min_fee = tx_size` shannons for any transaction, so the rounding never reaches zero under default settings. However:

- The configuration is explicitly user-settable and documented.
- The integration test template sets `min_fee_rate = 0` and several test specs use `FeeRate(100)`, demonstrating that low values are a supported and exercised configuration.
- A node operator who sets `min_fee_rate` to any value below `1000 / min_tx_size` (roughly `< 5` for a 200-byte transaction) silently loses the fee floor entirely, with no warning. [4](#0-3) [5](#0-4) 

---

### Recommendation

Round **up** instead of down when computing the minimum required fee, so that any non-zero `min_fee_rate` always produces at least 1 shannon of required fee for any non-zero transaction size:

```rust
// util/types/src/core/fee_rate.rs
pub fn fee(self, weight: u64) -> Capacity {
    // Round up: (a + b - 1) / b
    let fee = self.0.saturating_mul(weight).saturating_add(KW - 1) / KW;
    Capacity::shannons(fee)
}
```

Apply the same fix to `FeeRate::calculate` if it is used in admission-critical comparisons.

---

### Proof of Concept

**Setup**: node configured with `min_fee_rate = 4` (shannons/KW).

**Transaction**: a minimal ~200-byte CKB transaction where `inputs_capacity == outputs_capacity` (zero fee).

**Expected**: rejected with `PoolRejectedTransactionByMinFeeRate`.

**Actual**:
```
min_fee = FeeRate(4).fee(200) = 4 * 200 / 1000 = 0
fee (0) < min_fee (0)  →  false
→ transaction accepted into tx pool
```

The zero-fee transaction enters the pool. Repeating this with many transactions exhausts pool capacity (`max_tx_pool_size = 180 MB`) with transactions miners will never include, denying service to legitimate users. [1](#0-0) [6](#0-5) [7](#0-6)

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

**File:** tx-pool/src/pool.rs (L101-114)
```rust
    /// min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
        // don't account for duplicate txs
        let replaced_fees: HashMap<_, _> = conflicts
            .iter()
            .map(|c| (c.id.clone(), c.inner.fee))
            .collect();
        let replaced_sum_fee = replaced_fees
            .values()
            .try_fold(Capacity::zero(), |acc, x| acc.safe_add(*x));
        let res = replaced_sum_fee.map_or(Err(CapacityError::Overflow), |sum| {
            sum.safe_add(extra_rbf_fee)
        });
```

**File:** test/template/ckb.toml (L87-88)
```text
min_fee_rate = 0 # Here fee_rate are calculated directly using size in units of shannons/KB
min_rbf_rate = 0 # Here rbf_rate are calculated directly using size in units of shannons/KB
```

**File:** test/src/specs/tx_pool/replace.rs (L44-47)
```rust
    fn modify_app_config(&self, config: &mut ckb_app_config::CKBAppConfig) {
        config.tx_pool.min_rbf_rate = ckb_types::core::FeeRate(100);
        config.tx_pool.min_fee_rate = ckb_types::core::FeeRate(100);
    }
```
