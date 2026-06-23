### Title
Tx-Pool Admission Uses Size-Only Fee Check, Allowing Cheap Cycle-Budget Exhaustion Griefing - (File: tx-pool/src/util.rs)

### Summary
The tx-pool admission gate (`check_tx_fee`) enforces the minimum fee rate using only the transaction's **byte size**, while the actual resource cost of a transaction to the network is measured by its **weight** (`max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`). An unprivileged attacker can craft transactions with small byte size but near-maximum cycle consumption, paying only the size-based minimum fee (~200 shannons) while consuming cycle resources worth ~60× more in weight-equivalent terms. This allows cheap, sustained griefing of the tx-pool's cycle budget and block assembly quality.

---

### Finding Description

In `tx-pool/src/util.rs`, the `check_tx_fee` function enforces the minimum fee rate using only `tx_size` (serialized byte size):

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

However, the actual resource cost of a transaction is measured by `get_transaction_weight`, which takes the **maximum** of byte size and cycle-equivalent bytes:

```rust
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

The `fee_rate()` method on `TxEntry` — used for eviction and block assembly sorting — uses this weight-based calculation:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [3](#0-2) 

The pool eviction (`limit_size`) only triggers on **byte size** overflow (`total_tx_size > max_tx_pool_size`), not on cycle overflow, and evicts by weight-based fee rate: [4](#0-3) 

There is no separate cycle-budget cap on the pool's total admitted cycles — `total_tx_cycles` is tracked but never used as an eviction trigger: [5](#0-4) 

---

### Impact Explanation

**Concrete numbers** (using production defaults):

| Parameter | Value |
|---|---|
| `min_fee_rate` | 1,000 shannons/KW |
| `max_tx_verify_cycles` | 70,000,000 |
| `DEFAULT_BYTES_PER_CYCLES` | 0.000_170_571_4 |
| Cycle-equivalent bytes at max cycles | 70,000,000 × 0.000_170_571_4 ≈ **11,940 bytes** |
| Minimum realistic tx size | ~200 bytes |
| Weight of such a tx | max(200, 11,940) = **11,940** |
| Fee paid (size-based check) | 1,000 × 200 / 1,000 = **200 shannons** |
| Effective weight-based fee rate | 200 × 1,000 / 11,940 ≈ **16.7 shannons/KW** |
| Required weight-based fee rate | **1,000 shannons/KW** |
| **Underpayment ratio** | **~60×** | [6](#0-5) 

An attacker deploying a script that loops for ~70,000,000 cycles (stored in a cell dep, not in the transaction itself, keeping tx size small) can:

1. Submit transactions that pass the size-based admission check at 200 shannons each.
2. Each transaction consumes 70,000,000 cycles — the full per-tx cycle budget.
3. With a 180 MB pool and ~200-byte transactions, up to ~900,000 such transactions can be admitted.
4. Total attacker cost: ~180,000,000 shannons ≈ **1.8 CKB** to fill the pool.
5. The pool's `total_tx_cycles` accumulates to an enormous value, degrading block assembly: the block assembler must iterate through high-cycle, low-weight-fee-rate entries that consume the block's cycle limit (`max_block_cycles`) while contributing minimal fee. [7](#0-6) 

The block assembler selects transactions by weight-based fee rate, so attacker transactions are deprioritized for inclusion — but they still occupy pool space and cycle tracking, and the attacker can continuously resubmit evicted transactions to maintain pool pressure. [8](#0-7) 

---

### Likelihood Explanation

- **Entry path**: Any unprivileged user can call `send_transaction` via JSON-RPC or relay transactions via P2P. No special role or key is required.
- **Cost**: ~1.8 CKB to fill the 180 MB pool with maximum-cycle transactions. This is economically accessible.
- **Persistence**: The attacker must continuously resubmit as eviction removes low-fee-rate entries, but the cost per cycle of disruption is ~60× lower than for legitimate transactions.
- **Naturally occurring**: High-cycle scripts (e.g., complex lock scripts, ZK verifiers) submitted with minimum fees can trigger this condition without malicious intent. [1](#0-0) 

---

### Recommendation

Replace the size-only minimum fee check in `check_tx_fee` with a weight-based check that accounts for cycles. Since cycles are not known at the pre-check stage (they require script execution), the check should be applied **after** verification, using the actual measured cycles:

```rust
// After verify_rtx returns verified.cycles:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

This ensures that transactions with high cycle consumption pay proportionally higher fees, closing the gap between the admission check and the actual resource cost. The existing size-only check can be retained as a fast pre-filter before script execution. [9](#0-8) 

---

### Proof of Concept

1. Deploy a CKB script that executes a tight loop consuming exactly 69,999,999 cycles (just under `max_tx_verify_cycles = 70,000,000`). Store it in a live cell on-chain.

2. Craft a transaction:
   - One input cell (consuming a spendable UTXO)
   - One output cell (returning capacity minus fee)
   - One cell dep referencing the high-cycle script
   - Lock script = the high-cycle script
   - Serialized size ≈ 200 bytes
   - Fee = 200 shannons (exactly `min_fee_rate × tx_size = 1000 × 200 / 1000`)

3. Submit via `send_transaction` RPC. The node runs `check_tx_fee`:
   - `min_fee = 1000 * 200 / 1000 = 200 shannons` ✓ passes
   - Script executes, consuming ~70,000,000 cycles
   - `TxEntry` is created with `cycles ≈ 70,000,000`, `size ≈ 200`
   - Actual `fee_rate() = 200 * 1000 / max(200, 11940) ≈ 16.7 shannons/KW` — far below the 1,000 shannons/KW minimum

4. Repeat with ~900,000 such transactions (each spending a different UTXO, or using a chain of unconfirmed outputs up to `max_ancestors_count = 25`).

5. Observe: `tx_pool_info` shows `total_tx_cycles` at an enormous value; block templates contain few or no attacker transactions (correctly deprioritized by weight-based fee rate) but legitimate high-fee transactions are also crowded out by pool size pressure. [10](#0-9) [11](#0-10) [3](#0-2)

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

**File:** tx-pool/src/pool.rs (L290-329)
```rust
    // Remove transactions from the pool until total size <= size_limit.
    // Return a `Reject` for current inserting entry if it's removed
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
        ret
    }
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

**File:** resource/ckb.toml (L210-216)
```text
[tx_pool]
max_tx_pool_size = 180_000_000 # 180mb
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
max_tx_verify_cycles = 70_000_000
max_ancestors_count = 25
```

**File:** tx-pool/src/block_assembler/mod.rs (L443-446)
```rust
            let max_block_cycles = consensus.max_block_cycles();
            let (txs, _txs_size, _cycles) = tx_pool_reader
                .package_txs(max_block_cycles, txs_size_limit.expect("overflow checked"));
            txs
```

**File:** tx-pool/src/process.rs (L705-755)
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
