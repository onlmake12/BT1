Audit Report

## Title
Size-Only Fee Admission Check Allows High-Cycle Transactions Below `min_fee_rate` - (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` enforces the minimum fee rate using only the serialized byte size of the transaction, before script verification runs and actual cycles are known. Because no second weight-based fee check is performed after `verify_rtx` returns the real cycle count, an attacker can craft a compact-but-cycle-heavy transaction that passes admission at a fee rate up to ~59× below `min_fee_rate`, forcing the node to execute up to 70 M VM cycles per submission at negligible cost.

## Finding Description
The admission gate is `check_tx_fee`, called inside `pre_check` before any script execution:

```
pre_check → check_tx_fee (size-based) → verify_rtx (cycles returned) → TxEntry::new → submit_entry
```

`check_tx_fee` computes the minimum fee against raw serialized size only: [1](#0-0) 

The code comment at lines 42–44 explicitly labels this a "cheap check" approximation. After `verify_rtx` returns the actual `Completed { cycles, fee }` struct, `_process_tx` immediately constructs a `TxEntry` and calls `submit_entry` with no additional fee-rate validation: [2](#0-1) 

The weight-based fee rate (`get_transaction_weight`) is used everywhere else — ordering, eviction, and the `fee_rate()` method on `TxEntry` — but never as an admission gate: [3](#0-2) [4](#0-3) 

`get_transaction_weight` is defined as `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`: [5](#0-4) 

When cycles dominate, `weight >> size`, so the size-based admission check is far weaker than the weight-based fee rate the rest of the system enforces. The gap is not bounded by any subsequent check.

## Impact Explanation
With default `min_fee_rate = 1000 shannons/KW` and `max_tx_verify_cycles = 70,000,000`:

| Parameter | Value |
|---|---|
| `tx_size` | 200 bytes |
| `cycles` | 70,000,000 |
| `weight` | `max(200, 70M × 0.000170571)` = **11,940** |
| `min_fee` (admission) | `1000 × 200 / 1000` = **200 shannons** |
| Effective fee rate | `200 × 1000 / 11,940` ≈ **17 shannons/KW** |

The transaction passes admission (fee ≥ 200 shannons) but carries an effective fee rate ~59× below the configured floor. The node is forced to execute 70 M VM cycles of script verification per submission. Sustained flooding pollutes the pool with transactions that miners will never select (miners use weight-based ordering) and exhausts node CPU, matching the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**.

## Likelihood Explanation
The attack path requires no special privilege. Any actor with a live cell can:
1. Author a RISC-V lock script that spins for ~70 M cycles.
2. Build a transaction with serialized size ≤ 300 bytes spending that cell.
3. Set the fee to exactly `min_fee_rate × tx_size / 1000` shannons (e.g., 200–300 shannons).
4. Submit via the public `send_transaction` RPC. [6](#0-5) 

The default pool size (180 MB) and expiry (12 hours) bound the steady-state pool pollution but do not prevent sustained CPU exhaustion: each iteration costs ~300 shannons and forces ~70 M cycles of node CPU. The verify-queue workers process these serially, starving legitimate transactions. [7](#0-6) 

## Recommendation
After `verify_rtx` returns and actual cycles are known, perform a second fee-rate check using the real weight before constructing the `TxEntry`:

```rust
// in _process_tx, after verified_ret is unwrapped:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((
        Err(Reject::LowFeeRate(
            tx_pool_config.min_fee_rate,
            min_fee_by_weight.as_u64(),
            fee.as_u64(),
        )),
        snapshot,
    ));
}
```

This closes the gap between the cheap pre-verification size check and the weight-based ordering used everywhere else, ensuring every admitted transaction pays fees proportional to its actual resource consumption.

## Proof of Concept
1. Compile a CKB RISC-V lock script containing a tight counter loop consuming ~70 M cycles.
2. Build a transaction spending a live cell locked by that script; keep serialized size ≤ 300 bytes.
3. Set `inputs_capacity − outputs_capacity = 300 shannons` (fee = 300, just above `min_fee_rate × 300 / 1000 = 300`).
4. Call `send_transaction` on a node with default config.
5. Observe: the node accepts the transaction (size-based fee check passes at line 45–52 of `util.rs`), spends ~70 M cycles verifying it, and inserts it into the pool with effective fee rate ≈ 25 shannons/KW — well below the 1000 shannons/KW floor.
6. Repeat in a loop; each iteration costs ~300 shannons and forces ~70 M cycles of node CPU, with no rate-limiting mechanism beyond the pool size cap. [8](#0-7) [9](#0-8)

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

**File:** tx-pool/src/process.rs (L401-426)
```rust
    pub(crate) async fn process_tx(
        &self,
        tx: TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<Completed, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.verify_queue_contains(&tx).await || self.orphan_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }

        if let Some((ret, snapshot)) = self
            ._process_tx(tx.clone(), remote.map(|r| r.0), None)
            .await
        {
            self.after_process(tx, remote, &snapshot, &ret).await;
            ret
        } else {
            // currently, the returned cycles is not been used, mock 0 if delay
            Ok(Completed {
                cycles: 0,
                fee: Capacity::zero(),
            })
        }
    }
```

**File:** tx-pool/src/process.rs (L715-754)
```rust
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

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/verify_mgr.rs (L129-163)
```rust
            // pick a entry to run verify
            let entry = {
                let mut tasks = self.tasks.write().await;
                match tasks.pop_front(self.role == WorkerRole::OnlySmallCycleTx) {
                    Some(entry) => entry,
                    None => {
                        if !tasks.is_empty() {
                            tasks.re_notify();
                            debug!(
                                "Worker (role: {:?}) didn't got tx after pop_front, but tasks is not empty, notify other Workers now",
                                self.role
                            );
                        }
                        return;
                    }
                }
            };

            if let Some((res, snapshot)) = self
                .service
                ._process_tx(
                    entry.tx.clone(),
                    entry.remote.map(|e| e.0),
                    Some(&mut self.command_rx),
                )
                .await
            {
                self.service
                    .after_process(entry.tx, entry.remote, &snapshot, &res)
                    .await;
            } else {
                info!("_process_tx for tx: {} returned none", entry.tx.hash());
            }
        }
    }
```
