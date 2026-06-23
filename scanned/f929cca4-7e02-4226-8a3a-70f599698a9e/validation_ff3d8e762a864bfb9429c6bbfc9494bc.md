### Title
Fee Rate Minimum Check Uses Raw Byte Size Instead of Transaction Weight, Allowing Compute-Heavy Tx-Pool Spam - (`tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` enforces the `min_fee_rate` threshold using the raw serialized byte size of a transaction as the weight denominator. However, the rest of the system — block assembly sorting, fee rate statistics, and fee estimation — uses `get_transaction_weight(tx_size, cycles)`, which can be orders of magnitude larger than `tx_size` for compute-heavy transactions. This unit mismatch allows an unprivileged tx-pool submitter to inject high-cycle transactions at a fraction of the intended minimum fee cost, enabling cheap tx-pool resource exhaustion.

---

### Finding Description

CKB defines two distinct notions of transaction "size":

**1. Raw serialized byte size (`tx_size`):**
Used in `check_tx_fee` as the weight argument when computing the minimum required fee. [1](#0-0) 

The code itself acknowledges the mismatch with a comment: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check."*

**2. Transaction weight (`get_transaction_weight`):**
The actual weight used for block assembly prioritization, fee rate statistics, and fee estimation: [2](#0-1) 

Weight is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)` where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. For a transaction at the maximum allowed cycles (70,000,000), the weight is approximately `max(tx_size, 11,940)` — potentially 60× larger than a minimal-size transaction's byte count.

**The `FeeRate` type** is defined as shannons per kilo-weight: [3](#0-2) 

So `min_fee_rate.fee(tx_size)` and `min_fee_rate.fee(weight)` produce completely different values when `weight >> tx_size`.

**The divergence in practice:**

- `check_tx_fee` (admission gate): uses `tx_size` → `min_fee_rate.fee(tx_size as u64)` [4](#0-3) 
- `TxEntry::fee_rate()` (pool sorting/eviction): uses `get_transaction_weight(self.size, self.cycles)` [5](#0-4) 
- `get_fee_rate_statistics` RPC: uses `get_transaction_weight(*size, cycles)` [6](#0-5) 
- `estimate_fee_rate` RPC: returns weight-based fee rate via `FeeRate::as_u64()` [7](#0-6) 

The config comment reinforces the confusion — `min_fee_rate` is documented as "shannons/KB" (size-based) while `FeeRate` is defined as "shannons per kilo-weight": [8](#0-7) 

---

### Impact Explanation

An attacker crafts a transaction with:
- **Small serialized size** (e.g., 200 bytes) — minimizes the size-based fee gate
- **Maximum cycles** (e.g., 70,000,000) — maximizes actual weight (~11,940)

**Minimum fee required to pass `check_tx_fee`:**
`min_fee_rate.fee(200)` = 1,000 × 200 / 1,000 = **200 shannons**

**Minimum fee that would be required if weight were used correctly:**
`min_fee_rate.fee(11,940)` = 1,000 × 11,940 / 1,000 = **11,940 shannons**

The attacker pays ~60× less than the intended minimum to enter the pool. These transactions:
1. Consume significant script verification CPU (cycles) during pool admission
2. Occupy pool memory
3. Are deprioritized by miners (weight-based sorting) and may never be mined, persisting in the pool until expiry
4. Can be submitted in bulk to exhaust pool capacity (`max_tx_pool_size`) and verification worker threads

The `max_tx_verify_cycles` limit bounds the maximum weight ratio but does not eliminate the attack surface.

---

### Likelihood Explanation

This is reachable by any unprivileged tx-pool submitter via the standard `send_transaction` RPC. No special privileges, keys, or majority hashpower are required. The attacker only needs to construct a transaction whose lock or type script consumes many cycles while keeping the serialized transaction small (e.g., a script that performs heavy computation with minimal witness data). The attack is cheap and repeatable.

---

### Recommendation

Replace the size-based weight in `check_tx_fee` with the actual transaction weight. Since cycles are not yet known at the pre-verification admission stage, the check should either:

1. Use the declared cycles (if available from the cache entry) to compute `get_transaction_weight(tx_size, declared_cycles)` before calling `min_fee_rate.fee(weight)`, or
2. Apply a conservative weight estimate based on `max_tx_verify_cycles` as an upper bound for the size-based check, or
3. Re-run the fee rate check post-verification using the actual measured cycles.

The `FeeRate` type documentation and the `min_fee_rate` config comment should also be unified to use consistent units (shannons per kilo-weight throughout).

---

### Proof of Concept

**Setup:** Default config with `min_fee_rate = 1_000` (shannons/KB).

**Craft a transaction:**
- Serialized size: 200 bytes
- Script that consumes 70,000,000 cycles (max allowed)
- Fee: 200 shannons (just above `min_fee_rate.fee(200) = 200`)

**Admission path:**

```
check_tx_fee(tx_pool, snapshot, rtx, tx_size=200)
  → min_fee = min_fee_rate.fee(200) = 1000 * 200 / 1000 = 200 shannons  ✓ PASSES
```

**Actual weight-based fee rate of this transaction:**
```
weight = get_transaction_weight(200, 70_000_000)
       = max(200, 70_000_000 * 0.000_170_571_4)
       = max(200, 11_940) = 11_940

effective_fee_rate = FeeRate::calculate(200 shannons, 11_940)
                   = 200 * 1000 / 11_940 ≈ 16 shannons/kilo-weight
```

This is ~62× below the configured `min_fee_rate` of 1,000. The transaction enters the pool, consumes full verification resources, and is effectively free to spam at scale. [9](#0-8) [10](#0-9)

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

**File:** util/types/src/core/fee_rate.rs (L3-16)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;

impl FeeRate {
    /// Calculates the fee rate from a total fee and weight.
    pub fn calculate(fee: Capacity, weight: u64) -> Self {
        if weight == 0 {
            return FeeRate::zero();
        }
        FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
    }
```

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** rpc/src/util/fee_rate.rs (L103-105)
```rust
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
```

**File:** rpc/src/module/experiment.rs (L301-314)
```rust
    fn estimate_fee_rate(
        &self,
        estimate_mode: Option<EstimateMode>,
        enable_fallback: Option<bool>,
    ) -> Result<Uint64> {
        let estimate_mode = estimate_mode.unwrap_or_default();
        let enable_fallback = enable_fallback.unwrap_or(true);
        self.shared
            .tx_pool_controller()
            .estimate_fee_rate(estimate_mode.into(), enable_fallback)
            .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?
            .map_err(RPCError::from_any_error)
            .map(core::FeeRate::as_u64)
            .map(Into::into)
```

**File:** resource/ckb.toml (L212-214)
```text
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
```
