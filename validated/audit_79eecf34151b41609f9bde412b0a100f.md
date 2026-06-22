After thorough investigation of the CKB codebase, I found a valid analog to M-18's conditional minimum threshold bypass.

---

### Title
Tx-Pool Minimum Fee Rate Enforced Only on Serialized Size, Not Actual Transaction Weight — (`tx-pool/src/util.rs`)

### Summary

The `check_tx_fee` function enforces the `min_fee_rate` policy using only the transaction's **serialized byte size**, while the protocol's own weight formula is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. After `verify_rtx` resolves the actual cycle count, no weight-based fee rate re-check is performed. A transaction sender can craft a script-heavy (high-cycle), byte-light transaction that passes the size-based gate but carries an effective fee rate far below `min_fee_rate` when measured by weight.

### Finding Description

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum acceptable fee as:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { return Err(Reject::LowFeeRate(...)); }
``` [1](#0-0) 

The code's own comment acknowledges the approximation:

> "Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly" [2](#0-1) 

The actual transaction weight formula is defined as:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [3](#0-2) 

In `_process_tx`, the flow is:

1. `pre_check` → `check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)` — size-only gate
2. `verify_rtx(...)` → returns `verified.cycles` (actual cycle count now known)
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry built with real cycles
4. `submit_entry(...)` — no weight-based fee rate re-check [4](#0-3) 

After step 2, the actual weight is computable but never used to re-validate the fee rate. The "cheap check" is the only check.

The analog to M-18 is direct:

| M-18 (Ajna) | CKB analog |
|---|---|
| If `loansCount >= 10`, only average-based minimum is checked; absolute `quoteDust_` is skipped | If `cycles × factor > tx_size`, only size-based minimum is checked; weight-based minimum is skipped |
| Attacker draws debt below `quoteDust_` | Attacker submits tx with effective fee rate below `min_fee_rate` |

### Impact Explanation

An unprivileged RPC caller or relay peer can submit a transaction whose script consumes many cycles but whose serialized form is small. For example:

- `tx_size = 500` bytes → `min_fee = 500 × (1000/1000) = 500 shannons`
- `cycles = 50,000,000` → `weight = max(500, 50,000,000 × 0.000_170_571) = 8,528`
- Weight-based minimum fee would be `8,528 shannons`

A fee of 500 shannons passes `check_tx_fee` but represents an effective fee rate ~17× below `min_fee_rate`. Such transactions enter the pool, consume pool capacity, and can be used to degrade pool quality or evict legitimate transactions by flooding with script-heavy, fee-light entries. The `min_fee_rate` policy — the primary DoS protection for the tx-pool — is not enforced for this class of transaction.

### Likelihood Explanation

Any RPC caller with access to `send_transaction` can exploit this. Crafting a high-cycle, low-size transaction requires only a script that performs expensive computation (e.g., repeated hashing in a lock script) with a minimal witness. No privileged access, key material, or majority hashpower is required. The entry path is `rpc/src/module/pool.rs` → `send_transaction` → `_process_tx` → `pre_check` → `check_tx_fee`. [5](#0-4) 

### Recommendation

After `verify_rtx` returns the actual cycle count, compute the true transaction weight and re-validate the fee rate:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

This mirrors the recommendation in M-18: apply the absolute minimum check unconditionally, regardless of which branch of the conditional was taken.

### Proof of Concept

1. Deploy a lock script that performs 50,000,000 cycles of computation but requires only a 1-byte witness (small serialized size).
2. Create a transaction spending a cell locked by this script. The transaction's serialized size is ~500 bytes.
3. Set the fee to `min_fee_rate.fee(500) + 1` shannons (just above the size-based minimum).
4. Submit via `send_transaction`. The call succeeds: `check_tx_fee` passes because it uses `tx_size = 500`.
5. `verify_rtx` runs the script, consuming 50,000,000 cycles. The actual weight is ~8,528.
6. The transaction enters the pool with an effective fee rate of ~`500/8528 ≈ 59 shannons/KB`, far below the configured `min_fee_rate = 1000 shannons/KB`.
7. Repeat with many such transactions to fill the pool with sub-minimum-fee-rate entries. [6](#0-5) [7](#0-6) [8](#0-7)

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
