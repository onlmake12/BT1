### Title
Inconsistent Fee-Rate Weight Formula Between Tx-Pool Admission Check and Actual Fee-Rate Computation - (File: `tx-pool/src/util.rs`)

---

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` computes the minimum required fee using only `tx_size` as the weight, while `TxEntry::fee_rate()` in `tx-pool/src/component/entry.rs` computes the actual fee rate using `get_transaction_weight(size, cycles)` = `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions, this formula discrepancy allows an unprivileged tx-pool submitter to admit transactions whose actual fee rate is far below `min_fee_rate`, bypassing the pool's spam-prevention threshold.

---

### Finding Description

**Admission check** (`tx-pool/src/util.rs`, `check_tx_fee`):

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { return Err(Reject::LowFeeRate(...)); }
```

This computes: `min_fee = min_fee_rate × tx_size / 1000` [1](#0-0) 

**Actual fee-rate computation** (`tx-pool/src/component/entry.rs`, `TxEntry::fee_rate`):

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

This computes: `fee_rate = fee × 1000 / max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)` [2](#0-1) 

The weight function is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
``` [3](#0-2) 

Where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [4](#0-3) 

The root cause is that `check_tx_fee` is called during `pre_check` **before** script execution, so cycles are not yet known. After `verify_rtx` runs and cycles are determined, no second fee-rate check against `min_fee_rate` is performed using the actual weight. [5](#0-4) 

The code comment acknowledges the inconsistency but treats it as an intentional approximation:

> "Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly" [6](#0-5) 

The same size-only formula is also used in the RBF minimum-replace-fee calculation:

```rust
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
``` [7](#0-6) 

---

### Impact Explanation

An unprivileged tx-pool submitter can craft a transaction with:
- Small serialized size (e.g., 200 bytes)
- High cycle consumption (up to `max_tx_verify_cycles = 70,000,000`)

**Concrete example** with `min_fee_rate = 1,000 shannons/KW`:

| Metric | Value |
|---|---|
| `tx_size` | 200 bytes |
| `cycles` | 70,000,000 |
| `weight` = `max(200, 70M × 0.000_170_571_4)` | 11,940 bytes |
| Admission threshold: `1000 × 200 / 1000` | **200 shannons** |
| Actual fee rate: `200 × 1000 / 11,940` | **≈ 16.75 shannons/KW** |
| Ratio below `min_fee_rate` | **~60×** |

Such a transaction passes the admission gate, enters the pool, and consumes full script-execution resources (up to `max_tx_verify_cycles` cycles per transaction) while paying a fee rate ~60× below the configured minimum. The attacker can submit many such transactions to exhaust the verification worker pipeline and occupy pool capacity at a fraction of the intended cost.

---

### Likelihood Explanation

Any unprivileged RPC caller or P2P peer can submit transactions to the tx-pool. Crafting a small-serialized, high-cycle transaction requires only knowledge of a valid script (e.g., a loop script) and is straightforward. The `max_tx_verify_cycles` bound (70M) and `max_tx_pool_size` (180 MB) limit the total damage, and the eviction policy (lowest fee-rate evicted first) means these transactions are removed when the pool fills. However, the verification pipeline cost is paid before eviction, making this a viable resource-exhaustion vector against the verification workers.

---

### Recommendation

After `verify_rtx` completes and cycles are known, add a second fee-rate check using the actual weight:

```rust
let actual_weight = get_transaction_weight(tx_size, completed.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

This mirrors the pattern already used in `TxEntry::fee_rate()` and `AncestorsScoreSortKey` and closes the gap between the admission formula and the actual fee-rate formula.

---

### Proof of Concept

1. Deploy a script that loops for ~70,000,000 cycles.
2. Construct a transaction referencing that script with a small output (e.g., 200-byte serialized size).
3. Set the transaction fee to exactly `ceil(min_fee_rate × tx_size / 1000)` = 200 shannons (with `min_fee_rate = 1000`).
4. Submit via `send_transaction` RPC.
5. Observe: `check_tx_fee` passes (fee ≥ 200 shannons), `verify_rtx` runs the full 70M-cycle script, the entry is admitted with `fee_rate() ≈ 16.75 shannons/KW` — far below `min_fee_rate = 1000 shannons/KW`.
6. Repeat with many such transactions to saturate the verification worker queue while paying ~60× less fee than the configured minimum.

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
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

**File:** tx-pool/src/process.rs (L269-316)
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
    }
```

**File:** tx-pool/src/pool.rs (L103-103)
```rust
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```
