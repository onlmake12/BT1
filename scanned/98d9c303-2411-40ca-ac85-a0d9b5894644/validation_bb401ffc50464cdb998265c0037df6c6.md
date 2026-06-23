### Title
`check_tx_fee` omits cycles from weight in minimum fee rate enforcement, allowing high-cycles transactions to bypass the effective fee rate floor — (`tx-pool/src/util.rs`)

### Summary

The `check_tx_fee` function enforces the minimum fee rate using only serialized byte size as the transaction weight. However, the canonical weight used everywhere else in the tx-pool (sorting, eviction, fee-rate statistics) is `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. Because cycles are not yet known at pre-check time, the admission gate is permanently weaker than the pool's own fee-rate model. Any unprivileged RPC caller can craft a transaction that passes the size-only gate but carries an effective fee rate orders of magnitude below `min_fee_rate`.

### Finding Description

**Root cause — `tx-pool/src/util.rs`, `check_tx_fee`, line 45:**

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);   // ← cycles omitted
``` [1](#0-0) 

The canonical weight formula is defined in `util/types/src/core/tx_pool.rs`:

```rust
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

Every other consumer of fee rate — `TxEntry::fee_rate()`, `AncestorsScoreSortKey`, `EvictKey`, `FeeRateCollector::statistics()`, and both fee estimators — calls `get_transaction_weight(size, cycles)`: [3](#0-2) [4](#0-3) 

**Code flow that creates the gap:**

In `_process_tx` (`tx-pool/src/process.rs`):

1. `pre_check` → `check_tx_fee(tx_pool, snapshot, rtx, tx_size)` — size-only gate, cycles unknown.
2. `verify_rtx(...)` — actual cycles determined here.
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry created with real cycles.
4. `submit_entry(...)` — **no second fee-rate check using the now-known cycles**. [5](#0-4) 

The `pre_check` call site confirms only `tx_size` is passed: [6](#0-5) 

**Concrete numbers (mainnet defaults):**

| Parameter | Value |
|---|---|
| `min_fee_rate` | 1 000 shannons/KW |
| `max_tx_verify_cycles` | 70 000 000 |
| `DEFAULT_BYTES_PER_CYCLES` | 0.000 170 571 4 |
| Cycle-equivalent bytes at max cycles | ≈ 11 940 bytes |

A transaction with `tx_size = 100 bytes` and `cycles = 70 000 000`:

- **Size-only min fee** (what `check_tx_fee` enforces): `1000 × 100 / 1000 = 100 shannons`
- **Correct weight**: `max(100, 11 940) = 11 940`
- **Correct min fee**: `1000 × 11 940 / 1000 = 11 940 shannons`
- **Effective fee rate of admitted tx**: `100 × 1000 / 11 940 ≈ 8 shannons/KW` — **125× below `min_fee_rate`**

### Impact Explanation

An unprivileged RPC caller (`send_transaction`) can flood the tx-pool with transactions whose effective fee rate is up to ~125× below the configured `min_fee_rate`. These transactions:

1. Pass the admission gate and consume pool capacity (up to 180 MB by default).
2. Are sorted and evicted last-in-first-out only when the pool is full; if the pool is not full they persist until expiry (default 12 hours).
3. Crowd out legitimate transactions that pay the correct fee rate, delaying or preventing their confirmation.
4. Consume full script-verification resources on the node (up to `max_tx_verify_cycles` per tx) before the discrepancy is visible.

The attacker's cost is proportional to `min_fee_rate × tx_size`, not to the true weight, making the attack cheap relative to the damage.

### Likelihood Explanation

- Entry path: standard `send_transaction` RPC, available to any node peer or local caller.
- No special privilege, key, or majority hashpower required.
- Script authors can trivially construct a lock/type script that consumes near-`max_tx_verify_cycles` cycles in a small serialized transaction.
- The discrepancy is structural and reproducible on every node running default configuration.

### Recommendation

After `verify_rtx` returns the actual cycle count, perform a second fee-rate check using the correct weight before calling `submit_entry`:

```rust
// After verify_rtx, cycles are known:
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let effective_min_fee = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < effective_min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

This mirrors the pattern already used in `TxEntry::fee_rate()` and all pool-internal fee-rate consumers.

### Proof of Concept

```
1. Craft a CKB transaction with:
   - tx_size ≈ 100 bytes (minimal inputs/outputs)
   - A type/lock script that loops near max_tx_verify_cycles (70 000 000)
   - fee = 100 shannons  (= min_fee_rate × tx_size / 1000)

2. Submit via send_transaction RPC.

3. Observe: transaction is accepted (check_tx_fee passes: 100 >= 100).

4. Query pool entry via get_pool_tx_detail_info:
   - entry.fee_rate() = fee * 1000 / get_transaction_weight(100, 70_000_000)
                      = 100 * 1000 / 11940 ≈ 8 shannons/KW

5. Repeat with many UTXOs to fill the 180 MB pool with ~8 shannons/KW entries,
   blocking legitimate 1000 shannons/KW transactions from entering.
```

The structural gap is confirmed by the code comment itself acknowledging the theoretical incorrectness of the size-only check, combined with the absence of any post-verification fee-rate enforcement in `_process_tx`. [7](#0-6) [8](#0-7)

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

**File:** rpc/src/util/fee_rate.rs (L103-105)
```rust
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
```

**File:** tx-pool/src/process.rs (L274-290)
```rust
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
