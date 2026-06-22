### Title
Tx-Pool Minimum Fee Check Uses Only Serialized Size, Ignoring Cycles Weight — Allows High-Cycles Transactions to Bypass Minimum Fee Rate - (File: `tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size as the weight denominator. However, the actual transaction weight used everywhere else in the system is `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A transaction sender can craft a high-cycles, small-size transaction, pay a fee that only satisfies the size-based minimum, and have the transaction accepted into the pool with an effective fee rate far below `min_fee_rate`. This is the direct analog to the external report's inconsistent fee base: just as `marginFrom` could be zeroed out by restructuring inputs, the cycles component of the weight is silently excluded from the admission fee check.

---

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` computes the minimum required fee as:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

The code itself acknowledges the inconsistency. The weight denominator used here is `tx_size` (serialized bytes only). [1](#0-0) 

However, everywhere else in the system — fee-rate sorting, block assembly, fee estimation — the weight is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

with `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [3](#0-2) 

This weight is used for `TxEntry::fee_rate()`:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [4](#0-3) 

and for `AncestorsScoreSortKey` (transaction prioritization): [5](#0-4) 

The same inconsistency exists in `calculate_min_replace_fee` for RBF:

```rust
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
``` [6](#0-5) 

This also uses `size` only, not `get_transaction_weight(size, cycles)`, meaning a high-cycles replacement transaction also underpays the RBF surcharge.

**Root cause of the gap:** `check_tx_fee` is called before script verification (which determines actual cycles). After `verify_rtx` returns the actual cycles, a `TxEntry` is created with the correct cycles, but no second fee-rate check is performed against `min_fee_rate` using the actual weight. [7](#0-6) 

---

### Impact Explanation

A transaction sender can submit a transaction with:

- Small serialized size (e.g., 200 bytes)
- High cycles (up to `max_tx_verify_cycles = 70,000,000`)
- Fee just above `min_fee_rate.fee(200)` = 200 shannons (at default 1,000 shannons/KB)

The actual weight would be `max(200, 70,000,000 × 0.000_170_571_4) ≈ 11,940 bytes`, giving an effective fee rate of `200 / 11,940 × 1,000 ≈ 16.7 shannons/KB` — approximately **60× below the configured minimum**.

Such transactions:
1. Pass `check_tx_fee` and enter the pool
2. Consume significant validator CPU (up to 70M cycles per tx) during verification
3. Are sorted by the pool using their true (low) fee rate, crowding out legitimate transactions
4. Can be submitted in bulk to exhaust pool capacity and validator resources at a fraction of the intended cost

The `max_tx_pool_size` (180 MB) and `max_tx_verify_cycles` (70M) bound the per-transaction impact, but an attacker can submit many such transactions simultaneously via RPC or P2P relay. [8](#0-7) 

---

### Likelihood Explanation

Any unprivileged RPC caller (`send_transaction`) or P2P transaction relayer can exploit this. No special role, key, or majority hashpower is required. The attacker only needs to:

1. Write a CKB-VM script that consumes many cycles (e.g., a tight loop) but compiles to a small binary stored as a cell dep (not inline in the transaction itself, keeping serialized size small).
2. Submit the transaction with a fee just above `min_fee_rate × tx_size`.

This is a straightforward, low-cost attack requiring only standard CKB transaction submission access.

---

### Recommendation

After `verify_rtx` returns the actual cycles, perform a second fee-rate check using the true weight:

```rust
// Post-verification fee rate check using actual cycles
let actual_weight = get_transaction_weight(tx_size, completed.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

Apply the same fix to `calculate_min_replace_fee`, replacing `self.config.min_rbf_rate.fee(size as u64)` with `self.config.min_rbf_rate.fee(get_transaction_weight(size, cycles))`. [9](#0-8) 

---

### Proof of Concept

1. Deploy a CKB-VM script that runs a tight loop consuming ~69,000,000 cycles. Store it in a confirmed cell (so it is a cell dep, not inline — keeping the transaction's serialized size small, e.g., ~300 bytes).
2. Construct a transaction spending any live cell, referencing the loop script as a type script, with outputs capacity = inputs capacity − 300 shannons (fee = 300 shannons).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = min_fee_rate.fee(300) = 300 shannons` → passes.
5. `verify_rtx` executes the script, consuming ~69M cycles.
6. `TxEntry` is created with `cycles ≈ 69,000,000`, giving actual weight `max(300, 69,000,000 × 0.000_170_571_4) ≈ 11,769 bytes` and effective fee rate `300 / 11,769 × 1,000 ≈ 25.5 shannons/KB` — far below the 1,000 shannons/KB minimum.
7. The transaction is accepted into the pool. Repeat to flood the pool with computationally expensive, underpaying transactions. [10](#0-9) [11](#0-10)

### Citations

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

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/component/entry.rs (L221-231)
```rust
impl From<&TxEntry> for AncestorsScoreSortKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let ancestors_weight = get_transaction_weight(entry.ancestors_size, entry.ancestors_cycles);
        AncestorsScoreSortKey {
            fee: entry.fee,
            weight,
            ancestors_fee: entry.ancestors_fee,
            ancestors_weight,
        }
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

**File:** util/app-config/src/configs/tx_pool.rs (L11-22)
```rust
pub struct TxPoolConfig {
    /// Keep the transaction pool below <max_tx_pool_size> mb
    pub max_tx_pool_size: usize,
    /// txs with lower fee rate than this will not be relayed or be mined
    #[serde(with = "FeeRateDef")]
    pub min_fee_rate: FeeRate,
    /// txs need to pay larger fee rate than this for RBF
    #[serde(with = "FeeRateDef")]
    pub min_rbf_rate: FeeRate,
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
    /// max tx verify workers, default is 3/4 of cpu cores
```
