### Title
Tx-Pool Minimum Fee Rate Admission Check Uses Serialized Size Instead of Transaction Weight, Allowing Cycle-Heavy Transactions to Bypass the Intended Fee Floor — (File: `tx-pool/src/util.rs`)

---

### Summary

The tx-pool admission gate enforces `min_fee_rate` against `tx_size` (serialized byte count) only, while the actual resource cost of a transaction is `weight = max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A transaction with a small serialized size but near-maximum cycle consumption passes the size-only fee check while paying a tiny fraction of the fee that the weight-based floor would require. No post-verification fee-rate re-check using the actual measured cycles is ever performed, so the transaction is permanently admitted to the pool.

---

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` computes the minimum acceptable fee using only the serialized transaction size:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

This is the **only** fee-rate gate. The full processing pipeline in `_process_tx` is:

1. `pre_check` → calls `check_tx_fee(tx_pool, snapshot, rtx, tx_size)` — size-only check, cycles unknown.
2. `verify_rtx` → executes scripts, measures actual `verified.cycles`.
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry is created with real cycles.
4. `submit_entry` → entry is inserted into the pool. [2](#0-1) 

After step 2 the actual weight is known, but there is no second fee-rate check using `get_transaction_weight(tx_size, verified.cycles)`. The transaction is unconditionally admitted if it passed the size-only gate.

The canonical weight formula is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [3](#0-2) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [4](#0-3) 

**Concrete discrepancy:** With `min_fee_rate = 1000 shannons/KW` (the default) and `max_tx_verify_cycles = 70_000_000`:

| Metric | Value |
|---|---|
| Attacker tx serialized size | ~200 bytes |
| Attacker tx cycles | 70,000,000 |
| Size-based weight (used for admission) | 200 |
| Actual weight | max(200, 70,000,000 × 0.000170571) = **11,940** |
| Min fee required by size check | 200 shannons |
| Min fee that weight-based check would require | 11,940 shannons |
| **Effective fee-rate discount** | **~98.3%** | [5](#0-4) 

The `TxEntry::fee_rate()` method — used for pool prioritization and eviction — does use the correct weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [6](#0-5) 

So the pool correctly *deprioritizes* such transactions for block inclusion, but it still **admits** them, consuming verification CPU and pool memory at a deeply discounted fee.

---

### Impact Explanation

An unprivileged attacker submitting transactions via RPC (`send_transaction`) or P2P relay can:

1. **Force expensive script execution at near-zero cost.** A transaction with 70M cycles and 200 bytes pays ~200 shannons instead of ~11,940 shannons — a 59× discount on the node's CPU budget.
2. **Fill the tx pool with sub-minimum-fee-rate entries.** The pool's `max_tx_pool_size` (default 180 MB) is enforced by byte size, not by weight. High-cycle, small-size transactions occupy minimal byte space while consuming disproportionate verification resources.
3. **Degrade block assembly quality.** The pool's eviction and selection logic uses the correct weight-based fee rate, so these transactions will be ranked low and rarely mined, but they persist in the pool and consume resources. [7](#0-6) 

---

### Likelihood Explanation

- **Entry path is fully open:** `send_transaction` RPC is available to any local or remote caller; P2P relay accepts transactions from any connected peer.
- **No special knowledge required:** The attacker only needs to craft a transaction whose lock/type script loops for near-`max_tx_verify_cycles` cycles. Any RISC-V script that spins in a loop achieves this.
- **No funds at risk for the attacker:** The attacker pays a small fee (the size-based minimum) and gets the node to burn CPU.
- **Amplification scales with the cycles/size ratio**, which can be up to ~60× at the default `max_tx_verify_cycles = 70,000,000`.

---

### Recommendation

After `verify_rtx` returns the actual `verified.cycles`, perform a second fee-rate check using the true weight before calling `submit_entry`:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

This mirrors the existing size-only check in `check_tx_fee` but uses the post-verification weight. The size-only pre-check in `check_tx_fee` can remain as a cheap early-exit for obviously under-fee'd transactions.

---

### Proof of Concept

1. Deploy a lock script that executes a tight loop consuming ~69,000,000 cycles (just under `max_tx_verify_cycles = 70,000,000`).
2. Craft a transaction spending a cell locked by that script. Serialized size ≈ 200 bytes.
3. Set fee = `min_fee_rate * tx_size / 1000` = `1000 * 200 / 1000` = **200 shannons**.
4. Submit via `send_transaction` RPC.
5. **Expected (buggy) result:** Transaction is accepted into the pool. The node executes 69M cycles of script verification for 200 shannons — an effective fee rate of ~17 shannons/KW instead of the intended 1000 shannons/KW.
6. **Expected (correct) result:** The post-verification weight check would compute `weight = max(200, 69,000,000 × 0.000170571) ≈ 11,769`, require `min_fee = 11,769 shannons`, and reject the transaction with `LowFeeRate`. [8](#0-7) [9](#0-8)

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

**File:** util/app-config/src/legacy/tx_pool.rs (L10-16)
```rust
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
// default max ancestors count
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** util/app-config/src/configs/tx_pool.rs (L11-26)
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
    #[serde(default = "default_max_tx_verify_workers")]
    pub max_tx_verify_workers: usize,
    /// max ancestors size limit for a single tx
    pub max_ancestors_count: usize,
```
