### Title
Tx-Pool Min-Fee-Rate Gate Uses Serialized Size Instead of Weight, Allowing Below-Minimum-Fee-Rate Transactions to Enter the Pool — (`File: tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee by multiplying `min_fee_rate` (whose unit is **shannons per kilo-weight**) by the raw serialized byte size of the transaction, not by its true weight. Because weight is defined as `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`, any transaction whose cycle cost dominates its byte size will pass the admission gate with a fee that is below the configured minimum fee rate when measured correctly. The code itself acknowledges the discrepancy in a comment but treats the check as "good enough," leaving no subsequent weight-based rejection before the transaction is committed to the pool.

---

### Finding Description

`FeeRate` is documented and implemented as **shannons per kilo-weight**. [1](#0-0) 

`FeeRate::fee(weight)` computes `fee_rate * weight / 1000`. [2](#0-1) 

The canonical weight function is:

```
weight = max(tx_size_bytes, cycles × DEFAULT_BYTES_PER_CYCLES)
```

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [3](#0-2) 

The admission gate `check_tx_fee` passes `tx_size` (raw bytes) directly to `FeeRate::fee`, not the weight:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [4](#0-3) 

`check_tx_fee` is the only fee-rate gate called during `pre_check` before the transaction is admitted: [5](#0-4) 

After `verify_rtx` returns the actual cycle count, a `TxEntry` is created with both `size` and `cycles`. `TxEntry::fee_rate()` correctly uses `get_transaction_weight(size, cycles)`: [6](#0-5) 

But there is **no second rejection** based on the weight-corrected fee rate. The transaction is already in the pool.

---

### Impact Explanation

An attacker submits a transaction with:
- Small serialized size `S` (e.g., 200 bytes — minimal inputs/outputs)
- High cycle consumption `C` via a script that loops heavily

When `C × 0.000_170_571_4 > S`, the true weight `W > S`. The admission check computes:

```
min_fee = min_fee_rate × S / 1000   ← too low (uses size, not weight)
```

The correct threshold would be:

```
correct_min_fee = min_fee_rate × W / 1000   ← higher
```

A fee satisfying `min_fee ≤ fee < correct_min_fee` passes `check_tx_fee` and enters the pool. The attacker can flood the pool with computationally expensive transactions at below-minimum fee rates, consuming node CPU during script verification and degrading tx-pool quality for honest users. The pool's eviction and prioritization logic (which uses the correct weight-based fee rate) will eventually evict these entries, but only after they have already consumed verification resources.

---

### Likelihood Explanation

The vulnerability is reachable by any unprivileged RPC caller via `send_raw_transaction`. The attacker controls both the transaction's serialized size (by choosing minimal inputs/outputs) and its cycle consumption (by embedding a script that performs expensive computation). The code comment explicitly acknowledges the unit mismatch, confirming the gap is real and not a misread. The ratio between weight and size can be up to `MAX_BLOCK_CYCLES × DEFAULT_BYTES_PER_CYCLES / MAX_BLOCK_BYTES ≈ 1.0`, meaning for cycle-heavy transactions the undercount can be nearly 2×.

---

### Recommendation

Replace the size-based minimum fee check in `check_tx_fee` with a weight-based check. Since `check_tx_fee` is called before script execution (cycles are not yet known), use the declared cycle limit from the transaction or perform a two-phase check: a size-only pre-screen followed by a weight-corrected check after `verify_rtx` returns the actual cycle count. Concretely, after `verify_rtx` resolves `cycles`, recompute:

```rust
let weight = get_transaction_weight(tx_size, cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

and reject before `submit_entry`.

---

### Proof of Concept

Given `min_fee_rate = 1_000 shannons/KW` (the default):

| Parameter | Value |
|---|---|
| `tx_size` | 200 bytes |
| `cycles` | 10_000_000 |
| `weight` = max(200, 10_000_000 × 0.000_170_571_4) | **1_705** |
| `min_fee` (size-based, current check) | 1_000 × 200 / 1_000 = **200 shannons** |
| `correct_min_fee` (weight-based) | 1_000 × 1_705 / 1_000 = **1_705 shannons** |

A transaction paying 201 shannons passes `check_tx_fee` but has an actual fee rate of `201 × 1_000 / 1_705 ≈ 117 shannons/KW` — **8.5× below** the configured minimum. An attacker can submit thousands of such transactions, each requiring expensive script execution, at a fraction of the intended minimum cost. [7](#0-6) [8](#0-7) [2](#0-1)

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
