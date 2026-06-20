### Title
Fee Rate Minimum Floor Bypassed for High-Cycles Transactions via Size-Only Weight Approximation — (File: `tx-pool/src/util.rs`)

---

### Summary

The `check_tx_fee` admission gate in CKB's tx-pool computes the minimum required fee using only the transaction's serialized byte size as the weight, while the true transaction weight also accounts for consumed VM cycles. For a high-cycles, small-size transaction the actual weight can be an order of magnitude larger than the size alone, so the pre-admission fee floor is far too low. No post-verification fee-rate check using the real weight is ever performed, meaning such transactions are permanently admitted to the pool at a fraction of the intended minimum cost.

---

### Finding Description

**Root cause — `FeeRate::fee()` integer division floor:**

`FeeRate::fee()` computes the minimum fee as:

```
min_fee = fee_rate * weight / KW   (KW = 1000)
``` [1](#0-0) 

**Root cause — `check_tx_fee` uses size, not actual weight:**

The only fee-rate gate in the tx-pool admission path is:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { return Err(Reject::LowFeeRate(...)); }
``` [2](#0-1) 

The code itself acknowledges the approximation:

> "Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly" [3](#0-2) 

**The actual weight formula is:**

```
weight = max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)
``` [4](#0-3) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [5](#0-4) 

**No post-verification fee-rate check exists.** After `verify_rtx` returns the real cycle count, a `TxEntry` is created with the actual cycles, but `TxEntry::fee_rate()` (which uses the real weight) is never compared against `min_fee_rate` before the entry is submitted to the pool. [6](#0-5) 

**Concrete numbers with default configuration:**

| Parameter | Value |
|---|---|
| `DEFAULT_MIN_FEE_RATE` | 1000 shannons/KW |
| `max_tx_verify_cycles` | `TWO_IN_TWO_OUT_CYCLES × 20 ≈ 70 000 000` |
| Cycle-derived weight | `70 000 000 × 0.000_170_571_4 ≈ 11 940 bytes` |
| Attacker tx serialized size | 200 bytes |
| Fee charged (size-based check) | `1000 × 200 / 1000 = 200 shannons` |
| Fee that should be required | `1000 × 11 940 / 1000 = 11 940 shannons` |
| **Underpayment ratio** | **~60×** | [7](#0-6) 

---

### Impact Explanation

An unprivileged transaction sender can submit high-cycles, small-size transactions to the tx-pool while paying only the size-based minimum fee — roughly 60× less than the weight-based minimum the protocol intends to enforce. Consequences:

1. **Node resource exhaustion**: The node must run full CKB-VM verification (consuming up to `max_tx_verify_cycles` cycles) for each admitted transaction before discovering the fee rate is inadequate.
2. **Pool pollution**: The pool fills with transactions whose actual fee rate is far below `min_fee_rate`. These transactions will never be mined (miners sort by actual fee rate), but they occupy pool slots and delay legitimate transactions.
3. **Cheap DoS**: The cost to the attacker is proportional to `tx_size`, not to the actual VM work imposed on the node.

---

### Likelihood Explanation

- **Entry path**: Any RPC caller (`send_transaction`) or P2P peer relaying a transaction triggers `check_tx_fee`.
- **Craft requirement**: The attacker needs a transaction whose lock/type script consumes near-maximum cycles but whose serialized form is small. This is straightforward — a script that runs a tight loop or performs many hash operations fits the profile.
- **No special privilege required**: Standard unprivileged transaction sender.

---

### Recommendation

1. **Add a post-verification fee-rate check**: After `verify_rtx` returns the actual cycle count, recompute the minimum fee using `get_transaction_weight(tx_size, verified.cycles)` and reject if `fee < min_fee_rate.fee(actual_weight)`.
2. **Alternatively**, use a conservative upper-bound weight estimate in the pre-verification check: `get_transaction_weight(tx_size, max_tx_verify_cycles)` so that the cheap check is never more permissive than the real check.

---

### Proof of Concept

1. Construct a CKB transaction whose lock script runs a tight loop consuming `~70 000 000` cycles (within `max_tx_verify_cycles`). Keep the serialized transaction size at ~200 bytes (minimal inputs/outputs, no large args).
2. Set the fee to exactly `200 shannons` (passes `check_tx_fee` because `1000 × 200 / 1000 = 200`).
3. Submit via `send_transaction` RPC.
4. Observe: the node runs full VM verification (~70 M cycles), then admits the transaction. `TxEntry::fee_rate()` returns `FeeRate::calculate(200 shannons, 11940) = 16 shannons/KW`, far below the 1000 shannons/KW minimum.
5. Repeat with many such transactions to exhaust pool capacity and node verification bandwidth at ~60× below the intended cost floor. [8](#0-7) [9](#0-8)

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

**File:** util/types/src/core/tx_pool.rs (L279-279)
```rust
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;
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

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L9-10)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
```
