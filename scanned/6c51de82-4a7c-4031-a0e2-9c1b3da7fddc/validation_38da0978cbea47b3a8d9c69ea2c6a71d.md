### Title
Fee Rate Admission Check Uses Raw Transaction Size Instead of Actual Weight, Allowing Cycle-Heavy Transactions to Bypass `min_fee_rate` - (File: tx-pool/src/util.rs)

---

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using only `tx_size` (serialized bytes) as the weight. However, the canonical weight used everywhere else in the system is `get_transaction_weight(tx_size, cycles) = max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions the two values diverge by orders of magnitude, so the admission gate is far more permissive than the configured `min_fee_rate` intends. No second check is performed after the actual cycle count is known, so under-priced cycle-heavy transactions are permanently admitted to the pool.

---

### Finding Description

`check_tx_fee` is the sole fee-rate gate for pool admission:

```rust
// tx-pool/src/util.rs  lines 42-52
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

`FeeRate::fee(weight)` is defined as `fee_rate * weight / 1000` (shannons per kilo-weight):

```rust
// util/types/src/core/fee_rate.rs  lines 34-37
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;   // KW = 1000
    Capacity::shannons(fee)
}
```

The canonical weight function is:

```rust
// util/types/src/core/tx_pool.rs  lines 298-303
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64,
                  (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

`check_tx_fee` is called in `pre_check` before script execution, so cycles are not yet known:

```rust
// tx-pool/src/process.rs  lines 289-290
let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
Ok((tip_hash, rtx, status, fee, tx_size))
```

After `verify_rtx` determines the actual cycle count, the entry is submitted to the pool. There is no second fee-rate check using the real weight. The pool's ordering and eviction logic (`AncestorsScoreSortKey`, `EvictKey`) do use `get_transaction_weight(size, cycles)`, but those mechanisms only evict when the pool is full — they do not reject an already-admitted entry.

**Concrete example with `min_fee_rate = 1000` shannons/KW:**

| Parameter | Value |
|---|---|
| `tx_size` | 100 bytes |
| `cycles` | 10 000 000 |
| Actual weight | `max(100, 10 000 000 × 0.000_170_571_4)` = **1 705** |
| Actual min fee | `1000 × 1705 / 1000` = **1 705 shannons** |
| Check min fee | `1000 × 100 / 1000` = **100 shannons** |

A transaction paying 101 shannons passes `check_tx_fee` but has an effective fee rate of `101 × 1000 / 1705 ≈ 59 shannons/KW` — 17× below the configured minimum.

---

### Impact Explanation

An unprivileged transaction sender can permanently admit transactions to the mempool whose true fee rate is far below `min_fee_rate`. Each such transaction:

1. Forces the node to execute full script verification (proportional to `cycles`) before the under-payment is detectable.
2. Occupies pool memory until the pool is full and eviction runs.
3. Consumes CPU during pool-ordering operations.

Because the attacker pays only the size-proportional fee, the cost-to-impact ratio is highly asymmetric for cycle-heavy scripts (e.g., complex lock scripts, recursive type scripts). This constitutes a resource-exhaustion / DoS vector against the tx-pool and the verification worker threads.

---

### Likelihood Explanation

The attack is straightforward: submit a transaction whose script consumes many cycles but whose serialized size is small (e.g., a single input/output with a computationally expensive lock). The RPC endpoint `send_transaction` and the P2P relay path both funnel through `pre_check` → `check_tx_fee`, so the attack is reachable by any unprivileged caller. No special privilege, key material, or majority hash power is required.

---

### Recommendation

After `verify_rtx` returns the actual cycle count, perform a second fee-rate check using the true weight:

```rust
let actual_weight = get_transaction_weight(tx_size, completed.cycles);
let actual_min_fee = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < actual_min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

This mirrors the pattern used in pool ordering and eviction and closes the gap between the admission check and the rest of the system.

---

### Proof of Concept

1. Construct a CKB transaction with a lock script that consumes ~10 000 000 cycles but whose serialized size is ~100 bytes.
2. Set the output capacity so that `inputs_capacity − outputs_capacity = 101 shannons` (fee = 101 shannons).
3. Submit via `send_transaction` RPC to a node with `min_fee_rate = 1000`.
4. `check_tx_fee` computes `min_fee = 1000 × 100 / 1000 = 100 shannons`; fee 101 > 100 → **admitted**.
5. After script execution the actual weight is 1 705; the effective fee rate is ≈ 59 shannons/KW, well below the 1 000 shannons/KW minimum.
6. The transaction sits in the pool, having forced the node to spend ~10 000 000 cycles of verification for a 101-shannon fee.
7. Repeat with many such transactions to exhaust pool memory and verification worker capacity.

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
