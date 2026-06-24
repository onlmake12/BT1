Audit Report

## Title
Tx-Pool Min-Fee Check Uses Serialized Size Only, Ignoring Actual Cycles Weight, Allowing High-Cycles Transactions to Bypass Effective Fee-Rate Enforcement — (File: tx-pool/src/util.rs)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size, not its actual weight (`max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`). After `verify_rtx` resolves the true cycle count in `_process_tx`, no second fee-rate check is performed before `submit_entry`. An unprivileged submitter can craft a small-serialized, near-max-cycles transaction that passes the size-only fee gate at a fraction of the intended cost, enabling sustained pool flooding and CPU exhaustion at ~1/12 the intended fee floor.

## Finding Description

`check_tx_fee` explicitly acknowledges the gap in a code comment and uses only `tx_size` for the minimum fee calculation:

```rust
// tx-pool/src/util.rs lines 42-45
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The correct weight formula exists in `get_transaction_weight`:

```rust
// util/types/src/core/tx_pool.rs lines 298-303
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

In `_process_tx`, after `verify_rtx` resolves the actual cycle count, no second fee-rate check is performed before `submit_entry`:

```rust
// tx-pool/src/process.rs lines 751-753
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
``` [3](#0-2) 

`submit_entry` performs RBF checks and time-relative re-verification on tip change, but contains no fee-rate re-validation against actual weight. [4](#0-3) 

`TxEntry::fee_rate()` does correctly use `get_transaction_weight(self.size, self.cycles)`, but this is only used for sorting and eviction priority — not for admission gating. [5](#0-4) 

**Quantified gap:** With `max_tx_verify_cycles = 70,000,000` and `DEFAULT_BYTES_PER_CYCLES ≈ 0.000_170_571_4`, a transaction consuming 70 M cycles has an effective weight of ≈ 11,940 bytes. If its serialized size is 1,000 bytes, `check_tx_fee` requires only 1,000 shannons (at 1,000 shannons/KW), while the true weight-based minimum fee is 11,940 shannons — a ~12× shortfall.

## Impact Explanation

This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

An attacker can continuously submit small-serialized, near-max-cycles transactions that:
1. Pass the size-only fee gate at ~1/12 the intended cost.
2. Consume full script-execution CPU time during `verify_rtx` on every node that processes them.
3. Occupy pool slots (pool is byte-capped at 180 MB, not cycle-capped), so many such transactions fit simultaneously.
4. Displace legitimate transactions via pool eviction (which does use actual weight via `EvictKey`), causing confirmation delays for honest users.

The attacker can sustain this pressure indefinitely by resubmitting evicted transactions, at a cost far below the intended fee floor.

## Likelihood Explanation

Any node reachable via the `send_transaction` RPC or the P2P relay path is a valid entry point. No privileged access, key material, or majority hash power is required. Crafting a small-serialized, high-cycles transaction requires only a lock/type script that loops near `max_tx_verify_cycles`. The attack is cheap to automate and monitor, and the ~12× fee discount makes it economically attractive relative to the intended cost.

## Recommendation

After `verify_rtx` resolves the actual cycle count in `_process_tx`, perform a second fee-rate check using the true weight before calling `submit_entry`:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(
        tx_pool_config.min_fee_rate,
        min_fee_by_weight.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

This requires acquiring the tx pool config (already available via `self.tx_pool_config`) and does not require a lock, since `fee`, `tx_size`, and `verified.cycles` are all known at that point. Alternatively, enforce a cycles-proportional fee floor at the pre-check stage using the `declared_cycles` parameter already available in `_process_tx`.

## Proof of Concept

1. Craft a transaction with serialized size ≈ 1,000 bytes and a lock/type script that consumes ≈ 70,000,000 cycles (near `max_tx_verify_cycles`).
2. Set the fee to exactly `min_fee_rate.fee(1_000)` shannons (e.g., 1,000 shannons at the default 1,000 shannons/KW rate).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1,000 × 1,000 / 1,000 = 1,000 shannons` → passes.
5. `verify_rtx` executes the script, returning `cycles ≈ 70,000,000`.
6. Actual weight = `max(1,000, 70,000,000 × 0.000_170_571_4)` ≈ 11,940.
7. True required fee = `11,940 × 1,000 / 1,000 = 11,940 shannons` — but only 1,000 were paid.
8. `TxEntry::new(rtx, 70_000_000, 1_000_shannons, 1_000)` is created and submitted with no further fee check.
9. Repeat at scale to fill the pool with cycle-heavy, fee-light entries, displacing legitimate transactions and exhausting CPU on all processing nodes.

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
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

**File:** tx-pool/src/process.rs (L751-754)
```rust
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
