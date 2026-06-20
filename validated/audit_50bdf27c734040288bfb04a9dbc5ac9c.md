### Title
`check_tx_fee` Enforces Minimum Fee Rate Using Serialized Size While Actual Fee Rate Uses Weight, Allowing Compute-Heavy Transactions to Bypass the `min_fee_rate` Floor — (`tx-pool/src/util.rs`)

---

### Summary

The tx-pool admission check in `check_tx_fee` computes the minimum required fee using raw serialized transaction size (`tx_size`), while the actual fee rate stored in every `TxEntry` and used for pool ordering is computed using `get_transaction_weight(size, cycles) = max(size, cycles * DEFAULT_BYTES_PER_CYCLES)`. When a transaction is compute-heavy (cycles-dominant), `weight >> size`, so the admission threshold is far lower than the intended `min_fee_rate`. An unprivileged RPC caller or P2P relay peer can craft a small-but-cycle-heavy transaction that passes the size-based admission check while carrying an effective weight-based fee rate orders of magnitude below `min_fee_rate`.

---

### Finding Description

CKB's fee rate model uses a **weight** metric that accounts for both serialized size and VM cycles consumed: [1](#0-0) 

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [2](#0-1) 

Every `TxEntry` stored in the pool exposes its fee rate using this weight: [3](#0-2) 

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

However, the **admission gate** `check_tx_fee` — the only fee check executed before a transaction enters the pool — computes the minimum required fee using raw serialized size, not weight: [4](#0-3) 

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

The code comment itself acknowledges the discrepancy. The same size-only metric is used in the RBF extra-fee calculation: [5](#0-4) 

```rust
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
    let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```

This is called from `pre_check`, which is the single shared path for both RPC (`send_transaction`) and P2P relay (`submit_remote_tx`) entry points: [6](#0-5) 

---

### Impact Explanation

A transaction with small serialized size but high cycle consumption passes `check_tx_fee` with a fee that satisfies `min_fee_rate * size`, but its actual weight-based fee rate is `fee * 1000 / weight` where `weight = cycles * DEFAULT_BYTES_PER_CYCLES >> size`.

**Concrete example** with default config (`min_fee_rate = 1000 shannons/KW`, `max_tx_verify_cycles = 70,000,000`):

| Metric | Value |
|---|---|
| Serialized size | 200 bytes |
| Cycles consumed | 70,000,000 |
| Weight (`max(200, 70M × 0.000170571)`) | **11,940** |
| Fee required by `check_tx_fee` | `1000 × 200 / 1000 = 200 shannons` |
| Actual weight-based fee rate | `200 × 1000 / 11940 ≈ **16 shannons/KW**` |
| Ratio below minimum | **~62×** below `min_fee_rate` |

Such a transaction is admitted to the pool, occupies a cycle-heavy slot, and is sorted near the bottom of the pool (low effective fee rate). An attacker can flood the pool with compute-heavy, fee-minimal transactions, degrading the fee rate floor and potentially crowding out legitimate transactions that correctly pay the weight-based minimum.

---

### Likelihood Explanation

The attack is reachable by any unprivileged actor:
- **RPC path**: any local or trusted RPC caller invoking `send_transaction`
- **P2P relay path**: any connected peer relaying a transaction via `submit_remote_tx`

Both paths converge on `pre_check` → `check_tx_fee`. [7](#0-6) 

CKB-VM scripts can be designed to consume the maximum allowed cycles (`max_tx_verify_cycles = 70,000,000`) with minimal bytecode (small serialized size). This is a standard capability of any script author and requires no privileged access.

---

### Recommendation

Replace the size-based minimum fee computation in `check_tx_fee` with a weight-based one, consistent with how `TxEntry::fee_rate()` is computed:

```rust
// In tx-pool/src/util.rs, check_tx_fee:
// BEFORE (size-only):
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);

// AFTER (weight-based, consistent with TxEntry::fee_rate):
// Note: cycles are not yet known at pre_check time for remote txs with declared_cycles.
// Use declared_cycles (or max_block_cycles as upper bound) to compute weight.
let weight = get_transaction_weight(tx_size, declared_cycles_or_max);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

Similarly, `calculate_min_replace_fee` in `tx-pool/src/pool.rs` should use the entry's weight (already available as `entry.size` and `entry.cycles`) rather than raw size. [8](#0-7) [9](#0-8) [3](#0-2) 

---

### Proof of Concept

1. Craft a CKB transaction with a lock script that loops for ~70,000,000 cycles but whose serialized size is ~200 bytes (e.g., a tight RISC-V loop in a small binary loaded via `code_hash`).
2. Set the transaction fee to `200 shannons` (satisfies `min_fee_rate.fee(200) = 200` at 1000 shannons/KW).
3. Submit via `send_transaction` RPC.
4. Observe: the transaction is accepted into the pool despite its weight-based fee rate being `200 * 1000 / 11940 ≈ 16 shannons/KW`, far below the configured `min_fee_rate = 1000 shannons/KW`.
5. Repeat to fill the pool with compute-heavy, fee-minimal transactions.

The admission check at `tx-pool/src/util.rs:45` passes because it only checks `fee >= min_fee_rate.fee(tx_size)`, not `fee >= min_fee_rate.fee(weight)`. [10](#0-9) [11](#0-10)

### Citations

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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

**File:** tx-pool/src/pool.rs (L101-127)
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
        if let Ok(res) = res {
            Some(res)
        } else {
            let fees = conflicts.iter().map(|c| c.inner.fee).collect::<Vec<_>>();
            error!(
                "conflicts: {:?} replaced_sum_fee {:?} overflow by add {}",
                conflicts.iter().map(|e| e.id.clone()).collect::<Vec<_>>(),
                fees,
                extra_rbf_fee
            );
            None
        }
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
