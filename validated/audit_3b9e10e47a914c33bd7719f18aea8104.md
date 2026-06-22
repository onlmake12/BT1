### Title
Tx-Pool Minimum Fee Rate Check Uses Serialized Size Instead of Actual Transaction Weight, Allowing Below-Minimum Fee Rate Admission - (File: tx-pool/src/util.rs)

---

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size (`tx_size`), while the actual transaction weight — which governs fee rate for sorting, eviction, and block assembly — is `get_transaction_weight(size, cycles) = max(size, cycles * DEFAULT_BYTES_PER_CYCLES)`. An unprivileged submitter can craft a transaction with a small serialized size but high script execution cycles that passes the admission check with an effective fee rate far below the configured minimum.

---

### Finding Description

The minimum fee rate gate in `check_tx_fee` computes the required minimum fee as:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

where `tx_size` is the raw serialized size of the transaction in bytes. [1](#0-0) 

The code itself acknowledges the discrepancy with an inline comment:

> "Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly"

However, the actual weight used everywhere else in the pool — for sorting (`AncestorsScoreSortKey`), eviction (`EvictKey`), and fee rate reporting — is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
``` [2](#0-1) 

with `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [3](#0-2) 

The `pre_check` path calls `check_tx_fee` with `tx_size` before any script execution, so cycles are unknown at that point: [4](#0-3) 

After `pre_check` passes, `verify_rtx` runs the scripts and returns the actual `verified.cycles`. The `TxEntry` is then constructed with the actual cycles but the fee that was already approved against the size-only check:

```rust
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
``` [5](#0-4) 

There is **no second fee rate check** after cycles are known. `submit_entry` does not re-validate the fee rate against the actual weight. [6](#0-5) 

The `TxEntry::fee_rate()` method, used for eviction and sorting, correctly uses the full weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [7](#0-6) 

This means an entry can be admitted with a fee that satisfies `fee >= min_fee_rate * size` but whose actual fee rate `fee / get_transaction_weight(size, cycles)` is well below `min_fee_rate`.

---

### Impact Explanation

**Concrete example** with `min_fee_rate = 1000 shannons/KW`:

- Transaction: `size = 200 bytes`, `cycles = 5_000_000`
- Actual weight: `max(200, 5_000_000 × 0.000_170_571_4) ≈ max(200, 852) = 852`
- `min_fee` from `check_tx_fee`: `1000 × 200 / 1000 = 200 shannons` → passes with `fee = 200`
- Actual fee rate: `200 × 1000 / 852 ≈ 234 shannons/KW` — **4× below the minimum**

An attacker can:
1. **Exhaust node verification resources**: Transactions pass the cheap admission gate and trigger full `ContextualTransactionVerifier` + script execution, consuming CPU for transactions that would never meet the real fee rate threshold.
2. **Temporarily pollute the tx-pool**: Such entries are admitted and remain until the pool's `limit_size` eviction runs, which is triggered only when the pool is full. Until then, they occupy pool slots and affect ancestor/descendant fee rate accounting.
3. **Degrade block template quality**: Entries with inflated apparent fee (size-based) but low actual fee rate can influence the block assembler's transaction selection scoring.

---

### Likelihood Explanation

The attack is reachable by any unprivileged caller of the `send_transaction` RPC or any P2P peer relaying a transaction. No special privileges, keys, or majority hashpower are required. Crafting a transaction with a small serialized body but a script that consumes many cycles is straightforward — a loop-heavy lock script or type script achieves this. The gap between size-based and weight-based fee rate grows with cycle count, making the bypass more severe for compute-intensive scripts. Likelihood is **medium**: the attack requires deliberate crafting but is technically simple and has no on-chain cost beyond the (below-minimum) fee paid.

---

### Recommendation

After `verify_rtx` returns the actual `verified.cycles`, perform a second fee rate check using the true transaction weight before calling `submit_entry`:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

Alternatively, restructure `check_tx_fee` to accept an optional declared-cycles hint (available from the relay path) so that the weight-based check can be applied even before full verification when cycles are declared.

---

### Proof of Concept

1. Configure a CKB node with `tx_pool.min_fee_rate = 1000` (shannons/KW).
2. Construct a transaction with:
   - One input cell with capacity `C` shannons.
   - One output cell with capacity `C - 200` shannons (fee = 200 shannons).
   - A lock script that executes ~5,000,000 cycles (e.g., a tight RISC-V loop).
   - Serialized transaction size ≈ 200 bytes.
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200 shannons`; the transaction passes.
5. `verify_rtx` runs the script, consuming 5,000,000 cycles; actual weight = 852.
6. The entry is admitted with effective fee rate ≈ 234 shannons/KW, well below the 1000 shannons/KW minimum.
7. Repeat with many such transactions to exhaust verification threads and pollute the pool.

Relevant code path: `send_transaction` → `submit_local_tx` → `_process_tx` → `pre_check` / `check_tx_fee` (size-only gate) → `verify_rtx` (cycles revealed) → `TxEntry::new` (no re-check) → `submit_entry`. [8](#0-7) [9](#0-8)

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

**File:** tx-pool/src/process.rs (L96-170)
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

                // in a corner case, a tx with lower fee rate may be rejected immediately
                // after inserting into pool, return proper reject error here
                for evict in evicted {
                    let reject = Reject::Invalidated(format!(
                        "invalidated by tx {}",
                        evict.transaction().hash()
                    ));
                    self.callbacks.call_reject(tx_pool, &evict, reject);
                }

                tx_pool.remove_conflict(&entry.proposal_short_id());
                tx_pool
                    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
                    .map_or(Ok(()), Err)?;

                if !may_recovered_txs.is_empty() {
                    let self_clone = self.clone();
                    tokio::spawn(async move {
                        // push the recovered txs back to verify queue, so that they can be verified and submitted again
                        let mut queue = self_clone.verify_queue.write().await;
                        for tx in may_recovered_txs {
                            debug!("recover back: {:?}", tx.proposal_short_id());
                            let _ = queue.add_tx(tx, false, None);
                        }
                    });
                }
                Ok(())
            })
            .await;

        (ret, snapshot)
    }
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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
