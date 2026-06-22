### Title
Minimum Fee Rate Check Uses Raw Bytes Instead of Weight Units, Allowing Cycle-Heavy Transactions to Bypass Fee Rate Enforcement - (File: tx-pool/src/util.rs)

### Summary
In `tx-pool/src/util.rs`, the `check_tx_fee` function enforces the minimum fee rate by computing `min_fee = min_fee_rate.fee(tx_size)`, passing raw serialized byte size as the weight argument. However, `FeeRate` is defined as **shannons per kilo-weight**, where weight is `max(tx_size_bytes, cycles * DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions, the actual weight can be orders of magnitude larger than the byte size, making the admission check far too lenient. This is a direct unit/denomination mismatch: bytes are used where weight units are required.

### Finding Description

`FeeRate` is explicitly documented as "shannons per kilo-weight":

```rust
/// shannons per kilo-weight
pub struct FeeRate(pub u64);
```

The correct weight for a transaction is computed by `get_transaction_weight`:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. For a transaction consuming 70,000,000 cycles (the maximum), the cycle-derived weight component is `≈ 11,940` weight units, even if the serialized size is only 200 bytes.

However, the sole admission-time fee rate gate, `check_tx_fee`, passes raw `tx_size` (bytes) directly to `FeeRate::fee()`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

`FeeRate::fee` computes: `fee = self.0.saturating_mul(weight) / KW` where `KW = 1000`. So with `min_fee_rate = 1000 shannons/KW`:

| Metric | Correct (weight-based) | Actual check (bytes-based) |
|---|---|---|
| Weight | 11,940 | 200 |
| Min fee | 11,940 shannons | 200 shannons |

A transaction with fee = 201 shannons passes the check but has an actual fee rate of `≈ 16.8 shannons/KW`, far below the 1,000 shannons/KW minimum. No subsequent weight-based fee rate check exists in the admission path — `verify_rtx` / `ContextualTransactionVerifier` only returns the fee amount, not a fee rate gate. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation

An unprivileged tx-pool submitter (RPC caller or relay peer) can craft a transaction that:
1. Has a small serialized size (e.g., 200 bytes) but consumes near-maximum VM cycles (e.g., 70,000,000).
2. Pays a fee of just 201 shannons — passing the bytes-based check (`200 * 1000 / 1000 = 200 shannons` minimum).
3. Has an actual weight-based fee rate of `≈ 16.8 shannons/KW`, 60× below the configured minimum.

Such transactions are admitted to the tx-pool, relayed to peers (which apply the same flawed check), and may be included in blocks. This:
- Allows systematic bypass of the minimum fee rate protection for cycle-heavy scripts.
- Enables low-cost spam of the mempool and relay network with computationally expensive transactions.
- Reduces miner revenue by allowing below-minimum-fee-rate transactions to be mined.
- Degrades node performance by forcing full CKB-VM execution of high-cycle transactions that paid inadequate fees. [4](#0-3) 

### Likelihood Explanation

The attack is straightforward and requires no special privileges — any RPC caller or relay peer can submit transactions. The maximum cycle limit (70,000,000) is well-known and publicly documented. A script author can write a script that consumes near-maximum cycles with minimal transaction bytes. The discrepancy factor between bytes and weight can reach `11,940 / 200 ≈ 60×`, meaning the effective minimum fee is 60× lower than intended for maximally cycle-heavy transactions. [5](#0-4) 

### Recommendation

Replace the bytes-based minimum fee computation in `check_tx_fee` with the correct weight-based computation. Since cycles are available after script verification (not before), the check should either:

1. **Short term**: Use the declared cycles from the relay message (if available) or a conservative upper-bound estimate to compute weight before admission, then enforce the weight-based minimum fee:
   ```rust
   let weight = get_transaction_weight(tx_size, declared_cycles);
   let min_fee = tx_pool.config.min_fee_rate.fee(weight);
   ```

2. **Long term**: Restructure the admission pipeline so that the fee rate check occurs after script verification (when actual cycles are known), using `get_transaction_weight(tx_size, actual_cycles)` as the weight argument. This matches how `TxEntry::fee_rate()` already correctly computes the fee rate for sorting and eviction. [6](#0-5) 

### Proof of Concept

1. Craft a CKB transaction with a lock script that loops for ~70,000,000 cycles. The serialized transaction size is ~200 bytes.
2. Submit via RPC `send_transaction` with fee = 201 shannons.
3. The `check_tx_fee` gate computes `min_fee = 1000 * 200 / 1000 = 200 shannons`. Since `201 >= 200`, the transaction is admitted.
4. The actual weight = `max(200, 70_000_000 * 0.000_170_571_4) = 11,940`. The actual fee rate = `201 * 1000 / 11940 ≈ 16.8 shannons/KW`, which is 60× below the 1,000 shannons/KW minimum.
5. The transaction propagates to all peers (which apply the same flawed check) and may be mined. [7](#0-6) [8](#0-7)

### Citations

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

**File:** util/app-config/src/legacy/tx_pool.rs (L9-12)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
```
