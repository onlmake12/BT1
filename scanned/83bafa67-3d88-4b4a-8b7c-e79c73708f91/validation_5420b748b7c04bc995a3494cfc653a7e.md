### Title
Minimum Fee Rate Enforcement Uses Raw `tx_size` Instead of Computed `weight`, Allowing Below-Rate Transactions Into the Pool - (File: `tx-pool/src/util.rs`)

---

### Summary

In `check_tx_fee`, the minimum fee threshold is computed using the raw serialized byte size (`tx_size`) of a transaction rather than the correct **weight** (`max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`). The correct weight formula is defined and used everywhere else in the codebase, but the admission gate uses only `tx_size`. An unprivileged RPC caller can craft a cycle-heavy, byte-small transaction that passes the fee check while its true fee rate is far below `min_fee_rate`.

---

### Finding Description

`FeeRate` is defined as **shannons per kilo-weight**, where weight is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. [1](#0-0) [2](#0-1) 

The correct weight formula is used consistently in `TxEntry::fee_rate()`, `AncestorsScoreSortKey`, `EvictKey`, and the fee-rate statistics RPC: [3](#0-2) [4](#0-3) 

However, the tx-pool admission gate `check_tx_fee` computes the minimum fee using only `tx_size`, not weight:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [5](#0-4) 

This is called from `pre_check` for every submitted transaction: [6](#0-5) 

The code's own comment acknowledges the discrepancy but treats it as a deliberate "cheap check." The result is that the computed `min_fee` is `min_fee_rate × tx_size / 1000`, whereas the correct threshold is `min_fee_rate × max(tx_size, cycles × 0.000_170_571_4) / 1000`. For a cycle-heavy transaction, the actual weight can be orders of magnitude larger than `tx_size`, making the enforced minimum fee far too low.

---

### Impact Explanation

An attacker submits a transaction with:
- Small serialized size (e.g., 200 bytes)
- High declared cycles (e.g., 3,500,000,000 — near `MAX_BLOCK_CYCLES`)

The actual weight = `max(200, 3_500_000_000 × 0.000_170_571_4)` ≈ **596,999** weight units.

With `min_fee_rate = 1000 shannons/KW`:
- Enforced `min_fee` = `1000 × 200 / 1000` = **200 shannons**
- Correct `min_fee` = `1000 × 596_999 / 1000` = **596,999 shannons**

A transaction paying only 200 shannons passes the gate, but its true fee rate is `200 × 1000 / 596_999` ≈ **0.33 shannons/KW** — roughly 3000× below `min_fee_rate`. Such transactions fill the pool and consume block cycle budget while paying negligible fees, degrading node throughput and undermining the fee market.

---

### Likelihood Explanation

The entry path is the standard `send_transaction` RPC, reachable by any unprivileged peer or local user. No special privilege, key, or majority hashpower is required. The attacker only needs to know the node's `min_fee_rate` (publicly visible via `tx_pool_info` RPC) and craft a transaction with high cycles and small size. This is straightforward with any CKB script that performs heavy computation.

---

### Recommendation

Replace `tx_size as u64` with the actual weight in `check_tx_fee`:

```rust
use ckb_types::core::tx_pool::get_transaction_weight;

pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
    cycles: Cycle,          // add cycles parameter
) -> Result<Capacity, Reject> {
    let fee = ...;
    let weight = get_transaction_weight(tx_size, cycles);
    let min_fee = tx_pool.config.min_fee_rate.fee(weight);
    ...
}
```

The `cycles` value is available after script verification completes. For the pre-check (before cycles are known), the check should use `tx_size` only as a lower-bound guard, and a second, accurate check should be performed post-verification using the declared/actual cycles.

---

### Proof of Concept

1. Node configured with `min_fee_rate = 1000` shannons/KW.
2. Craft a transaction: serialized size = 200 bytes, declared cycles = 3,500,000,000.
3. Set fee = 201 shannons (just above `min_fee_rate.fee(200)` = 200 shannons).
4. Submit via `send_transaction` RPC.
5. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200`, fee 201 > 200 → **accepted**.
6. Actual fee rate = `201 × 1000 / 596_999` ≈ **0.34 shannons/KW**, far below `min_fee_rate`.
7. Repeat to flood the pool with near-zero-fee-rate, cycle-saturating transactions. [7](#0-6) [2](#0-1) [8](#0-7)

### Citations

**File:** util/types/src/core/fee_rate.rs (L3-7)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** rpc/src/util/fee_rate.rs (L103-106)
```rust
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
```

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

**File:** tx-pool/src/process.rs (L274-290)
```rust
        let tx_size = tx.data().serialized_size_in_block();

        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

                // Try normal path first, if double-spending check success we don't need RBF check
                // this make sure RBF won't introduce extra performance cost for hot path
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
```
