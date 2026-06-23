### Title
Insufficient Fee Rate Validation: Size-Only Check Allows High-Cycles Transactions to Bypass `min_fee_rate` — (File: `tx-pool/src/util.rs`)

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` validates the minimum fee rate using only the serialized transaction size (`tx_size`) as the weight denominator. The actual weight used by miners and the eviction mechanism is `get_transaction_weight(tx_size, cycles) = max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For a high-cycles transaction, the actual weight can be orders of magnitude larger than the size alone. After `verify_rtx` returns the true cycle count, `_process_tx` in `tx-pool/src/process.rs` creates the `TxEntry` and submits it to the pool without re-checking the fee rate against the actual weight. A transaction sender can craft a transaction that passes the size-based admission check but carries an effective fee rate far below `min_fee_rate`, causing the transaction to be accepted into the pool but never mined — leaving the sender's input cells soft-locked until pool expiry.

---

### Finding Description

`check_tx_fee` explicitly acknowledges the discrepancy with a code comment:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The actual weight formula used everywhere else in the codebase is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`.

The full transaction processing pipeline in `_process_tx` is:

1. `pre_check` → calls `check_tx_fee` with `tx_size` only (cycles unknown at this point)
2. `verify_rtx` → actual cycles now known
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` → entry created with real cycles
4. `submit_entry` → entry inserted into pool [3](#0-2) 

There is **no second fee-rate check** after step 2 using `get_transaction_weight(tx_size, verified.cycles)`. The fee accepted during `pre_check` is carried forward unchanged into the pool entry.

`TxEntry::fee_rate()` — used by the eviction and block-assembly scoring — does use the correct weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [4](#0-3) 

So the admission gate uses size-only weight, but the eviction gate uses the correct weight — creating a window where a transaction can enter the pool but be permanently deprioritized by miners.

---

### Impact Explanation

**Concrete numbers** (using default config: `min_fee_rate = 1000 shannons/KW`, `max_tx_verify_cycles = 70_000_000`):

| Parameter | Value |
|---|---|
| `tx_size` | 500 bytes |
| `cycles` | 70,000,000 |
| Size-based min fee | `1000 × 500 / 1000 = 500 shannons` |
| Actual weight | `max(500, 70_000_000 × 0.000_170_571_4) ≈ 11,940` |
| Actual fee rate at 500 shannons | `500 / 11,940 × 1000 ≈ 42 shannons/KW` |

A transaction paying exactly 500 shannons passes `check_tx_fee` (500 ≥ 500) but has an actual fee rate of ~42 shannons/KW — 24× below `min_fee_rate`. Miners select transactions using `AncestorsScoreSortKey` which calls `get_transaction_weight`, so this transaction will never be selected for a block template. [5](#0-4) 

The transaction remains in the pool until `expiry_hours` (default 12 hours) elapses or until the pool overflows and the eviction mechanism removes it (eviction also uses actual weight, so it is evicted first). During this window, the sender's input cells are soft-locked: any attempt to spend them in a new transaction creates a conflict that requires RBF or waiting for expiry.

Additionally, an attacker can use this to fill the pool with high-cycles, low-fee transactions that pass admission but are immediately deprioritized, degrading pool quality for legitimate users.

---

### Likelihood Explanation

The entry path is fully unprivileged: any user can call `send_transaction` RPC or relay a transaction over P2P. Crafting a high-cycles transaction requires writing a CKB script (lock or type) that consumes many cycles — this is within the capability of any script author and is the normal use case for complex scripts (e.g., ZK verifiers, multi-sig schemes). The discrepancy is largest for scripts near `max_tx_verify_cycles` (70M cycles), which is a documented and supported limit. [6](#0-5) 

---

### Recommendation

After `verify_rtx` returns the actual cycle count in `_process_tx`, add a second fee-rate check using the true weight before creating the `TxEntry`:

```rust
let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

// Re-check fee rate against actual weight (cycles now known)
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee {
    return Some((
        Err(Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64())),
        snapshot,
    ));
}

let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
``` [7](#0-6) 

Alternatively, `check_tx_fee` could be updated to accept an optional `cycles` parameter and use `get_transaction_weight` when cycles are available.

---

### Proof of Concept

1. Write a CKB lock script that loops to consume ~70M cycles (near `max_tx_verify_cycles`).
2. Create a transaction spending a cell locked by this script. Set `tx_size ≈ 500 bytes`.
3. Set the fee to exactly `ceil(min_fee_rate × tx_size / 1000) = 500 shannons`.
4. Submit via `send_transaction` RPC.
5. **Expected**: `check_tx_fee` passes (500 ≥ 500 shannons size-based threshold). Transaction enters the pool.
6. **Observed**: `TxEntry::fee_rate()` returns ~42 shannons/KW. The transaction is never selected by `TxSelector::txs_to_commit` for a block template. It remains in the pool for 12 hours, soft-locking the input cell. [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** tx-pool/src/util.rs (L28-53)
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

**File:** tx-pool/src/process.rs (L269-316)
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
    }
```

**File:** tx-pool/src/process.rs (L715-753)
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

**File:** util/app-config/src/configs/tx_pool.rs (L21-21)
```rust
    pub max_tx_verify_cycles: Cycle,
```

**File:** tx-pool/src/component/tx_selector.rs (L97-162)
```rust
    pub fn txs_to_commit(
        mut self,
        size_limit: usize,
        cycles_limit: Cycle,
    ) -> (Vec<TxEntry>, usize, Cycle) {
        let mut size: usize = 0;
        let mut cycles: Cycle = 0;
        let mut consecutive_failed = 0;

        let mut iter = self
            .pool_map
            .sorted_proposed_iter()
            .filter(|entry| {
                entry.ancestors_size <= size_limit && entry.ancestors_cycles <= cycles_limit
            })
            .peekable();
        loop {
            let mut using_modified = false;

            if let Some(entry) = iter.peek()
                && self.skip_proposed_entry(&entry.proposal_short_id())
            {
                iter.next();
                continue;
            }

            // First try to find a new transaction in `proposed_pool` to evaluate.
            let tx_entry: TxEntry = match (iter.peek(), self.modified_entries.next_best_entry()) {
                (Some(entry), Some(best_modified)) => {
                    if &best_modified > entry {
                        using_modified = true;
                        best_modified.clone()
                    } else {
                        // worse than `proposed_pool`
                        iter.next().cloned().expect("peek guard")
                    }
                }
                (Some(_), None) => {
                    // Either no entry in `modified_entries`
                    iter.next().cloned().expect("peek guarded")
                }
                (None, Some(best_modified)) => {
                    // We're out of entries in `proposed`; use the entry from `modified_entries`
                    using_modified = true;
                    best_modified.clone()
                }
                (None, None) => {
                    break;
                }
            };

            let short_id = tx_entry.proposal_short_id();
            let next_size = size.saturating_add(tx_entry.ancestors_size);
            let next_cycles = cycles.saturating_add(tx_entry.ancestors_cycles);

            if next_cycles > cycles_limit || next_size > size_limit {
                consecutive_failed += 1;
                if using_modified {
                    self.modified_entries.remove(&short_id);
                    self.failed_txs.insert(short_id.clone());
                }
                if consecutive_failed > MAX_CONSECUTIVE_FAILURES {
                    break;
                }
                continue;
            }
```
