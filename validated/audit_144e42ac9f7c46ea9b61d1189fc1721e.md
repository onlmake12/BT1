### Title
Tx-Pool Admission Uses Size-Only Fee Check While Actual Fee Rate Uses Weight (Cycles-Aware) — (`tx-pool/src/util.rs`)

### Summary

The tx-pool admission check in `check_tx_fee` computes the minimum required fee using only the transaction's serialized byte size. However, the actual fee rate used for eviction, block-assembly scoring, and fee estimation throughout the rest of the tx-pool uses `weight = max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A transaction sender can craft a transaction with a small serialized size but high script-execution cycles, pay a fee that satisfies the size-based admission check, yet have a true weight-based fee rate far below `min_fee_rate`. The node admits the transaction after spending significant CPU on script verification, then immediately marks it as the lowest-priority candidate for eviction.

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` enforces the minimum fee rate using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { return Err(Reject::LowFeeRate(...)); }
``` [1](#0-0) 

The comment itself acknowledges the approximation. The actual fee rate used everywhere else in the pool is computed via `get_transaction_weight`:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
``` [2](#0-1) 

`TxEntry::fee_rate()` uses this weight-based calculation for eviction and scoring:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [3](#0-2) 

The `EvictKey` and `AncestorsScoreSortKey` both derive from this weight-based fee rate: [4](#0-3) [5](#0-4) 

The two metrics diverge maximally when cycles dominate. With `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` and `max_tx_verify_cycles = 70_000_000`:

- Maximum weight from cycles ≈ `70,000,000 × 0.000_170_571_4 ≈ 11,940 bytes`
- A 200-byte transaction with 70 M cycles has `weight ≈ 11,940`
- Size-based `min_fee = 1000 × 200 / 1000 = 200 shannons` → **passes admission**
- Weight-based fee rate = `200 / 11,940 × 1000 ≈ 16.7 shannons/KW` → **far below `min_fee_rate = 1000`** [6](#0-5) 

There is no second fee-rate check after `verify_rtx` completes and the actual cycle count is known. The entry is unconditionally admitted. [7](#0-6) 

### Impact Explanation

An unprivileged transaction sender can repeatedly submit transactions that:

1. Pass the size-based `check_tx_fee` gate with a minimal fee.
2. Force the node to execute up to `max_tx_verify_cycles` (70 M) cycles of script verification.
3. Enter the pool with a weight-based fee rate ~60× below `min_fee_rate`.
4. Immediately become the top eviction candidate, wasting all verification work.

The attacker pays only the size-based minimum fee per transaction while imposing the full cycle-verification cost on the node. Because the pool evicts by weight-based fee rate, these transactions are removed as soon as the pool fills, but the CPU cost of verifying them has already been paid. The cycle repeats indefinitely. This constitutes a CPU-exhaustion DoS reachable by any RPC caller or P2P peer that can submit transactions.

### Likelihood Explanation

The attack requires no special privilege — any `send_transaction` RPC caller or peer relaying transactions can trigger it. Crafting a small transaction that runs a tight loop in CKB-VM to consume near-maximum cycles is straightforward. The cost to the attacker is only the size-based minimum fee (e.g., 200 shannons per transaction), while the node pays 70 M cycles of verification per submission. The inconsistency is structural and present in every node running the default configuration.

### Recommendation

After `verify_rtx` returns the actual cycle count, re-check the fee rate using the true weight:

```rust
let weight = get_transaction_weight(tx_size, completed.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

This mirrors the fix applied in the Overlay report: the gating check must account for the same cost metric used by the rest of the system. Alternatively, enforce a cycles-proportional fee floor at the pre-check stage by using a conservative upper-bound cycle estimate derived from transaction structure before full execution.

### Proof of Concept

1. Construct a CKB transaction with:
   - A single input/output (serialized size ≈ 200 bytes).
   - A lock script that executes a tight loop consuming ≈ 70,000,000 cycles.
   - Fee = `ceil(min_fee_rate × tx_size / 1000)` = 200 shannons (passes size-based check).
2. Submit via `send_transaction` RPC to a node with default config (`min_fee_rate = 1000`).
3. Observe: the node accepts the transaction (no `LowFeeRate` rejection), runs full script verification consuming ~70 M cycles, admits the entry, then immediately marks it as the lowest-priority eviction candidate with effective fee rate ≈ 16 shannons/KW.
4. Repeat in a loop. Each iteration costs the attacker 200 shannons and forces the node to spend ~70 M cycles of verification work. [8](#0-7) [3](#0-2) [9](#0-8)

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

**File:** util/types/src/core/tx_pool.rs (L276-309)
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

/// The maximum size of the tx-pool to accept transactions
/// The ckb consensus does not limit the size of a single transaction,
/// but if the size of the transaction is close to the limit of the block,
/// it may cause the transaction to fail to be packed
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
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

**File:** tx-pool/src/component/entry.rs (L234-247)
```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);

        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            timestamp: entry.timestamp,
            descendants_count: entry.descendants_count,
        }
    }
```
