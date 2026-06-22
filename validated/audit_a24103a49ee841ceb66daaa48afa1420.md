### Title
Tx-Pool Minimum Fee Admission Check Omits Cycles Component of Transaction Weight, Allowing Underpayment - (`tx-pool/src/util.rs`)

---

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` enforces the minimum fee rate using only the serialized transaction size (`tx_size`) as the weight denominator. However, CKB's actual transaction weight is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. A transaction with high cycle consumption but small serialized size passes the admission check by paying only the size-based minimum fee, which can be orders of magnitude less than the fee required by the true weight. This is the direct CKB analog of the external report's missing-premium-component in the minimum execution cost calculation.

---

### Finding Description

CKB's fee model uses a unified "weight" to convert the two-dimensional block resource limits (bytes and cycles) into a single comparable unit:

```rust
// util/types/src/core/tx_pool.rs
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [1](#0-0) 

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`, derived from `MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES`. [2](#0-1) 

All internal fee-rate calculations — pool scoring, eviction, fee estimation — use this correct weight:

```rust
// tx-pool/src/component/entry.rs
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [3](#0-2) 

However, the **admission gate** — `check_tx_fee` — uses only `tx_size`, explicitly skipping the cycles component:

```rust
// tx-pool/src/util.rs
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [4](#0-3) 

`check_tx_fee` is called before `verify_rtx` (script execution), so cycles are not yet known at that point. After `verify_rtx` returns the actual cycles and the `TxEntry` is constructed with them, **no second fee check is performed** against the weight-based minimum. The entry proceeds directly to `submit_entry`: [5](#0-4) 

The same omission exists in `calculate_min_replace_fee` for RBF: the extra RBF fee is computed from `size` alone, not from `weight`:

```rust
// tx-pool/src/pool.rs
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
    let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
    ...
}
``` [6](#0-5) 

---

### Impact Explanation

**Concrete underpayment example** with default config (`min_fee_rate = 1000` shannons/KW, `max_tx_verify_cycles = 70_000_000`):

| Parameter | Value |
|---|---|
| `tx_size` | 100 bytes |
| `cycles` | 70,000,000 |
| Actual weight | `max(100, 70_000_000 × 0.000_170_571_4) ≈ 11,940` |
| Min fee checked (size-based) | `1000 × 100 / 1000 = 100 shannons` |
| Min fee required (weight-based) | `1000 × 11,940 / 1000 = 11,940 shannons` |
| **Underpayment factor** | **~119×** |

A transaction paying only 100 shannons enters the pool and can be mined, consuming nearly the full block cycle budget while paying ~1% of the fee that the weight-based rate demands. This:

1. Allows a tx-pool submitter to consume block cycle capacity at far below the enforced minimum fee rate.
2. Distorts the fee market: miners selecting by fee rate (using correct weight) will rank these transactions very low, but they still occupy pool space and can be mined by miners who do not apply the weight-based filter.
3. The same underpayment applies to RBF replacements via `calculate_min_replace_fee`.

---

### Likelihood Explanation

The attack is trivially reachable by any unprivileged user via the `send_transaction` RPC or P2P relay. The attacker only needs to craft a transaction whose lock or type script consumes near-maximum cycles (e.g., a script with a tight computation loop) while keeping the serialized transaction small (minimal inputs, outputs, witnesses). No special privilege, key, or majority hashpower is required. The `max_tx_verify_cycles` limit (70M by default) bounds the maximum underpayment factor but does not prevent it. [7](#0-6) 

---

### Recommendation

After `verify_rtx` returns the actual cycle count, perform a second fee check using the true weight:

```rust
let weight = get_transaction_weight(tx_size, cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

Apply the same fix to `calculate_min_replace_fee`, replacing `size` with `get_transaction_weight(size, cycles)` for the `extra_rbf_fee` computation. [8](#0-7) 

---

### Proof of Concept

1. Craft a CKB transaction with:
   - A lock script that runs a tight RISC-V loop consuming ~70,000,000 cycles.
   - A single input and single output, keeping serialized size ≈ 100 bytes.
   - Fee = 100 shannons (passes `min_fee_rate.fee(100) = 100`).

2. Submit via `send_transaction` RPC.

3. `check_tx_fee` passes: `fee (100) >= min_fee_rate.fee(tx_size=100) = 100`. [9](#0-8) 

4. `verify_rtx` executes the script, returns `cycles ≈ 70_000_000`. [10](#0-9) 

5. `TxEntry` is created with `cycles = 70_000_000`; `fee_rate()` computes `FeeRate::calculate(100, 11940) ≈ 8 shannons/KW` — far below `min_fee_rate = 1000`. No rejection occurs. [3](#0-2) 

6. The transaction is admitted to the pool and is eligible for mining, consuming ~11,940 weight units of block capacity while paying only 100 shannons — a ~119× underpayment relative to the enforced minimum fee rate.

### Citations

**File:** util/types/src/core/tx_pool.rs (L276-279)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
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

**File:** tx-pool/src/util.rs (L85-131)
```rust
pub(crate) async fn verify_rtx(
    snapshot: Arc<Snapshot>,
    rtx: Arc<ResolvedTransaction>,
    tx_env: Arc<TxVerifyEnv>,
    cache_entry: &Option<CacheEntry>,
    max_tx_verify_cycles: Cycle,
    command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
) -> Result<Completed, Reject> {
    let consensus = snapshot.cloned_consensus();
    let data_loader = snapshot.as_data_loader();

    if let Some(completed) = cache_entry {
        TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
            .verify()
            .map(|_| *completed)
            .map_err(Reject::Verification)
    } else if let Some(command_rx) = command_rx {
        ContextualTransactionVerifier::new(
            Arc::clone(&rtx),
            consensus,
            data_loader,
            Arc::clone(&tx_env),
        )
        .verify_with_pause(max_tx_verify_cycles, command_rx)
        .await
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
    } else {
        block_in_place(|| {
            ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
                .verify(max_tx_verify_cycles, false)
                .and_then(|result| {
                    DaoScriptSizeVerifier::new(
                        rtx,
                        snapshot.cloned_consensus(),
                        snapshot.as_data_loader(),
                    )
                    .verify()?;
                    Ok(result)
                })
                .map_err(Reject::Verification)
        })
    }
```

**File:** tx-pool/src/process.rs (L96-137)
```rust
    pub(crate) async fn submit_entry(
        &self,
        pre_resolve_tip: Byte32,
        entry: TxEntry,
        mut status: TxStatus,
    ) -> (Result<(), Reject>, Arc<Snapshot>) {
        let (ret, snapshot) = self
            .with_tx_pool_write_lock(move |tx_pool, snapshot| {
                // check_rbf must be invoked in `write` lock to avoid concurrent issues.
                let conflicts = if tx_pool.enable_rbf() {
                    tx_pool.check_rbf(&snapshot, &entry)?
                } else {
                    // RBF is disabled but we found conflicts, return error here
                    // after_process will put this tx into conflicts_pool
                    let conflicted_outpoint =
                        tx_pool.pool_map.find_conflict_outpoint(entry.transaction());
                    if let Some(outpoint) = conflicted_outpoint {
                        return Err(Reject::Resolve(OutPointError::Dead(outpoint)));
                    }
                    HashSet::new()
                };

                // if snapshot changed by context switch we need redo time_relative verify
                let tip_hash = snapshot.tip_hash();
                if pre_resolve_tip != tip_hash {
                    debug!(
                        "submit_entry {} context changed. previous:{} now:{}",
                        entry.proposal_short_id(),
                        pre_resolve_tip,
                        tip_hash
                    );

                    // destructuring assignments are not currently supported
                    status = check_rtx(tx_pool, &snapshot, &entry.rtx)?;

                    let tip_header = snapshot.tip_header();
                    let tx_env = status.with_env(tip_header);
                    time_relative_verify(snapshot, Arc::clone(&entry.rtx), tx_env)?;
                }

                let may_recovered_txs = self.process_rbf(tx_pool, &entry, &conflicts);
                let evicted = _submit_entry(tx_pool, status, entry.clone(), &self.callbacks)?;
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

**File:** util/app-config/src/legacy/tx_pool.rs (L9-14)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```
