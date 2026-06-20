### Title
Tx-Pool Minimum Fee Check Uses Serialized Size Instead of Weight, Allowing High-Cycle Transactions to Bypass the Effective Fee Rate Floor - (File: tx-pool/src/util.rs)

### Summary
`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only `tx_size` (the serialized byte size), while the actual fee rate stored in the pool and used for block selection is computed via `get_transaction_weight(tx_size, cycles)` = `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For any transaction whose cycle-equivalent weight exceeds its byte size, the admission check uses the smaller (optimistic) of the two resource dimensions, allowing the transaction to enter the pool with an effective fee rate far below the configured minimum.

### Finding Description
CKB blocks are bounded by two independent limits: `MAX_BLOCK_BYTES` (597,000 bytes) and `MAX_BLOCK_CYCLES` (3,500,000,000 cycles). To reduce this two-dimensional knapsack to a single dimension, `get_transaction_weight` normalises cycles to bytes using `DEFAULT_BYTES_PER_CYCLES = MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES ≈ 0.000170571` and returns the larger of the two:

```rust
// util/types/src/core/tx_pool.rs:298-303
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

Every downstream consumer — `TxEntry::fee_rate()`, `AncestorsScoreSortKey`, `EvictKey`, `FeeRateCollector`, and both fee estimators — uses this weight when computing fee rate.

The admission gate, however, uses only `tx_size`:

```rust
// tx-pool/src/util.rs:42-52
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

The comment acknowledges the discrepancy but treats it as an acceptable approximation. For a transaction where `cycles * DEFAULT_BYTES_PER_CYCLES >> tx_size`, the admission check passes while the actual stored fee rate is a fraction of `min_fee_rate`.

**Concrete numbers** (default config: `min_fee_rate = 1000 shannons/KW`, `max_tx_verify_cycles = 70,000,000`):

| Parameter | Value |
|---|---|
| `tx_size` | 100 bytes |
| `cycles` | 70,000,000 |
| `cycles_equivalent` | 70,000,000 × 0.000170571 ≈ 11,940 bytes |
| `weight` | max(100, 11,940) = 11,940 bytes |
| Required fee (size-based check) | 1000 × 100 / 1000 = **100 shannons** |
| Actual fee rate stored in pool | 100 × 1000 / 11,940 ≈ **8.4 shannons/KW** |
| Ratio below minimum | ~119× |

The transaction is admitted with a fee rate 119× below the configured floor.

### Impact Explanation
1. **Minimum fee rate bypass**: Any transaction sender can craft a transaction with small serialized size but near-maximum cycles, pay only the size-based minimum fee, and have the transaction accepted into the pool. The effective fee rate is far below `min_fee_rate`.
2. **Mempool pollution**: An attacker can fill the pool with such transactions. Although they are eventually evicted when the pool is full (eviction uses weight-based fee rate), they displace legitimate transactions during the window before eviction.
3. **Fee estimator distortion**: Both `ConfirmationFraction` and `WeightUnitsFlow` estimators call `get_transaction_weight` when a transaction is accepted (`accept_tx`), so they record the true low fee rate. This can skew fee estimates downward for other users.
4. **Block cycle consumption at below-floor fee rate**: A miner willing to include low-fee-rate transactions can pack such transactions, consuming the cycles budget without the sender paying the fee rate the operator intended to enforce.

### Likelihood Explanation
The entry path is fully unprivileged: any RPC caller can invoke `send_transaction` or `test_tx_pool_accept`. No special role, key, or configuration is required. Crafting a transaction with high cycles and small size is straightforward using any CKB script that performs heavy computation. The discrepancy grows linearly with cycles, so the maximum effect is achieved at `max_tx_verify_cycles`.

### Recommendation
Replace the size-only check in `check_tx_fee` with the weight-based check that is consistent with the rest of the fee rate subsystem:

```rust
// tx-pool/src/util.rs
use ckb_types::core::tx_pool::get_transaction_weight;

pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
    cycles: Cycle,          // add cycles parameter
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(...)
        .transaction_fee(rtx)?;
    let weight = get_transaction_weight(tx_size, cycles);
    let min_fee = tx_pool.config.min_fee_rate.fee(weight);
    if fee < min_fee {
        return Err(Reject::LowFeeRate(...));
    }
    Ok(fee)
}
```

The `cycles` value is available at the call sites in `process.rs` after `verify_rtx` returns a `Completed` result. Alternatively, if a pre-verification cheap check is still desired, the check should at minimum use `get_transaction_weight` with the declared (pre-verification) cycle count.

### Proof of Concept
1. Construct a CKB transaction with a lock script that loops for ~70,000,000 cycles but whose serialized size is ~100 bytes (e.g., a minimal input/output with a tight loop in the witness script).
2. Submit via `send_transaction` RPC.
3. The admission check computes `min_fee = 1000 * 100 / 1000 = 100 shannons`; pay exactly 100 shannons.
4. The transaction is accepted. Query `get_pool_tx_detail_info` — the stored fee rate will be `FeeRate::calculate(100, 11940) ≈ 8 shannons/KW`, well below the 1000 shannons/KW floor.
5. Repeat with many such transactions to demonstrate pool pollution at below-floor fee rates.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/process.rs (L269-315)
```rust
    pub(crate) async fn pre_check(
        &self,
        tx: &TransactionView,
    ) -> (Result<PreCheckedTx, Reject>, Arc<Snapshot>) {
        // Acquire read lock for cheap check
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
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
                        if conflicts.is_none() {
                            // this mean one input's outpoint is dead, but there is no direct conflicted tx in tx_pool
                            // we should reject it directly and don't need to put it into conflicts pool
                            error!(
                                "{} is resolved as Dead, but there is no conflicted tx",
                                rtx.transaction.proposal_short_id()
                            );
                            return Err(Reject::Resolve(OutPointError::Dead(out)));
                        }
                        // we also return Ok here, so that the entry will be continue to be verified before submit
                        // we only want to put it into conflicts pool after the verification stage passed
                        // then we will double-check conflicts txs in `submit_entry`

                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(err) => Err(err),
                }
            })
            .await;
        (ret, snapshot)
```
