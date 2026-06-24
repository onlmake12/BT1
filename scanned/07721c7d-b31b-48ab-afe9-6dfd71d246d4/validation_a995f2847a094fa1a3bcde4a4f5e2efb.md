Audit Report

## Title
Tx-Pool Admission Uses Size-Only Fee Check, Allowing Cheap Cycle-Budget Exhaustion Griefing - (File: tx-pool/src/util.rs)

## Summary
`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size, while the actual resource cost is measured by `get_transaction_weight` (which takes `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`). An unprivileged attacker can craft transactions with small byte size (~200 bytes) but near-maximum cycle consumption (~70M cycles), paying only the size-based minimum fee while consuming cycle resources worth ~60× more in weight-equivalent terms. This enables cheap, sustained griefing of the tx-pool's cycle budget and block assembly quality.

## Finding Description

**Root cause — size-only admission gate:**

In `tx-pool/src/util.rs` at line 45, `check_tx_fee` computes the minimum fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code comment explicitly acknowledges this is a deliberate approximation. The check runs during `pre_check`, before script execution, so cycles are not yet known. [1](#0-0) 

**Weight-based cost model used everywhere else:**

`get_transaction_weight` in `util/types/src/core/tx_pool.rs` computes the true resource cost as `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`: [2](#0-1) 

`TxEntry::fee_rate()` uses this weight for eviction and block assembly sorting: [3](#0-2) 

The `EvictKey` and `AncestorsScoreSortKey` both use weight-based fee rate: [4](#0-3) 

**Eviction only triggers on byte-size overflow, not cycle overflow:**

`limit_size` in `tx-pool/src/pool.rs` only evicts when `total_tx_size > max_tx_pool_size`: [5](#0-4) 

`total_tx_cycles` is tracked in `pool_map` but is never used as an eviction trigger: [6](#0-5) 

**Exploit flow:**

1. Attacker deploys a script that loops for ~70M cycles (stored in a cell dep, keeping tx size small).
2. Attacker crafts a transaction: ~200 bytes serialized, fee = 200 shannons (exactly `1000 × 200 / 1000`).
3. `check_tx_fee` passes: `min_fee = 1000 × 200 / 1000 = 200 shannons` ✓.
4. Script executes during `verify_rtx`, consuming ~70M cycles. `TxEntry` is created with `cycles ≈ 70M`, `size ≈ 200`.
5. Actual `fee_rate() = 200 × 1000 / max(200, 11940) ≈ 16.7 shannons/KW` — far below the 1,000 shannons/KW minimum.
6. Attacker repeats with ~900,000 transactions (chaining up to `max_ancestors_count = 1000` deep per UTXO chain, requiring ~900 initial UTXOs). [7](#0-6) 

**Why existing guards are insufficient:**

- The eviction mechanism (`limit_size`) correctly evicts attacker transactions by weight-based fee rate when the pool is full by byte size. However, the attacker can continuously resubmit evicted transactions at 200 shannons each, maintaining pool pressure at ~60× lower cost than legitimate high-cycle transactions.
- There is no cycle-budget cap that would reject or evict transactions when `total_tx_cycles` exceeds a threshold.
- The block assembler correctly deprioritizes attacker transactions, but they still occupy pool space and force eviction of legitimate low-to-medium fee transactions. [8](#0-7) 

## Impact Explanation

This matches the **High** impact category: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."

With production defaults (`min_fee_rate = 1000`, `max_tx_pool_size = 180MB`, `max_tx_verify_cycles = 70M`):
- Weight of a 200-byte, 70M-cycle tx: `max(200, 70M × 0.000_170_571_4) = 11,940`
- Fee paid: 200 shannons; fee required at weight-based rate: ~11,940 shannons
- Underpayment ratio: ~60×
- Cost to fill 180MB pool: ~1.8 CKB

Legitimate transactions with low-to-medium fees are continuously evicted to make room for attacker transactions, degrading block assembly quality and creating sustained network congestion at minimal attacker cost.

## Likelihood Explanation

- **Entry path**: Any unprivileged user via `send_transaction` RPC or P2P relay. No special role or key required.
- **Cost**: ~1.8 CKB to fill the pool initially; continuous resubmission at 200 shannons per evicted transaction.
- **Persistence**: Attacker must resubmit as eviction removes low-fee-rate entries, but the cost per cycle of disruption is ~60× lower than for legitimate transactions.
- **Naturally occurring**: High-cycle scripts (ZK verifiers, complex lock scripts) submitted with minimum fees can trigger this condition without malicious intent.

## Recommendation

After `verify_rtx` returns `verified.cycles`, apply a second weight-based minimum fee check before creating the `TxEntry`:

```rust
// In _process_tx, after verified cycles are known:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

The existing size-only check in `check_tx_fee` can be retained as a fast pre-filter before script execution. The post-verification weight-based check closes the gap between admission cost and actual resource cost.

## Proof of Concept

1. Deploy a CKB script that executes a tight loop consuming exactly 69,999,999 cycles. Store it in a live cell on-chain.
2. Craft a transaction: one input, one output, one cell dep referencing the high-cycle script, serialized size ≈ 200 bytes, fee = 200 shannons.
3. Submit via `send_transaction` RPC. Observe:
   - `check_tx_fee` passes: `min_fee = 1000 × 200 / 1000 = 200 shannons` ✓
   - Script executes, consuming ~70M cycles
   - `TxEntry` created with `cycles ≈ 70M`, `size ≈ 200`
   - `fee_rate() ≈ 16.7 shannons/KW` — far below the 1,000 shannons/KW minimum
4. Repeat with ~900 initial UTXOs, chaining up to 1,000 ancestors each, to fill the 180MB pool.
5. Observe via `tx_pool_info`: `total_tx_cycles` at an enormous value; legitimate low-to-medium fee transactions are evicted; block templates contain few or no attacker transactions (correctly deprioritized) but pool pressure forces eviction of legitimate transactions. [9](#0-8) [10](#0-9)

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

**File:** util/types/src/core/tx_pool.rs (L279-303)
```rust
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

**File:** tx-pool/src/pool.rs (L298-299)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
```

**File:** tx-pool/src/component/pool_map.rs (L68-75)
```rust
    // sum of all tx_pool tx's virtual sizes.
    pub(crate) total_tx_size: usize,
    // sum of all tx_pool tx's cycles.
    pub(crate) total_tx_cycles: Cycle,
    pub(crate) pending_count: usize,
    pub(crate) gap_count: usize,
    pub(crate) proposed_count: usize,
}
```

**File:** tx-pool/src/component/pool_map.rs (L710-729)
```rust
    /// Calculate size and cycles statistics for adding a tx.
    fn updated_stat_for_add_tx(
        &self,
        tx_size: usize,
        cycles: Cycle,
    ) -> Result<(usize, Cycle), Reject> {
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_size {} overflows by add {}",
                self.total_tx_size, tx_size
            ))
        })?;
        let total_tx_cycles = self.total_tx_cycles.checked_add(cycles).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_cycles {} overflows by add {}",
                self.total_tx_cycles, cycles
            ))
        })?;
        Ok((total_tx_size, total_tx_cycles))
    }
```

**File:** tx-pool/src/process.rs (L705-754)
```rust
    pub(crate) async fn _process_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Option<Cycle>,
        command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
    ) -> Option<(Result<Completed, Reject>, Arc<Snapshot>)> {
        let wtx_hash = tx.witness_hash();
        let instant = Instant::now();
        let is_sync_process = command_rx.is_none();

        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);

        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

        let verified_ret = verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            command_rx,
        )
        .await;

        let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

        if let Some(declared) = declared_cycles
            && declared != verified.cycles
        {
            info!(
                "process_tx declared cycles not match verified cycles, declared: {}, verified: {}, tx_hash: {}",
                declared,
                verified.cycles,
                tx.hash()
            );
            return Some((
                Err(Reject::DeclaredWrongCycles(declared, verified.cycles)),
                snapshot,
            ));
        }

        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
        try_or_return_with_snapshot!(ret, submit_snapshot);
```

**File:** util/app-config/src/legacy/tx_pool.rs (L9-20)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
// default max ancestors count
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
// Default expiration time for pool transactions in hours
const DEFAULT_EXPIRY_HOURS: u8 = 12;
// Default max_tx_pool_size 180mb
const DEFAULT_MAX_TX_POOL_SIZE: usize = 180_000_000;
```
