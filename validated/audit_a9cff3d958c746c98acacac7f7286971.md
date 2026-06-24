All code claims verified against the actual repository. Every cited line, function, and behavior matches exactly.

- `check_tx_fee` at [1](#0-0)  uses only `tx_size`, with the comment explicitly acknowledging it is a "cheap check."
- `get_transaction_weight` at [2](#0-1)  uses `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`.
- `_process_tx` at [3](#0-2)  constructs `TxEntry::new` and calls `submit_entry` with no second fee rate check after `verified.cycles` is known.
- `submit_entry` at [4](#0-3)  performs RBF and time-relative checks but no weight-based fee rate re-validation.
- `TxEntry::fee_rate()` at [5](#0-4)  correctly uses full weight for eviction/sorting, confirming the admitted entry carries a below-minimum effective fee rate.

---

Audit Report

## Title
Tx-Pool Minimum Fee Rate Check Uses Serialized Size Instead of Actual Transaction Weight, Allowing Below-Minimum Fee Rate Admission - (File: tx-pool/src/util.rs)

## Summary
`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate gate using only the transaction's serialized byte size. The actual weight used for sorting, eviction, and fee rate reporting is `max(size, cycles * DEFAULT_BYTES_PER_CYCLES)`. Because no second fee rate check is performed after `verify_rtx` reveals the actual cycle count, a transaction with a small serialized size but high script execution cycles can be permanently admitted to the pool with an effective fee rate far below the configured minimum.

## Finding Description
`check_tx_fee` computes the minimum required fee as:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code itself documents the discrepancy. The actual weight used everywhere else in the pool is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

The full `_process_tx` flow is:
1. `pre_check` → `check_tx_fee` with `tx_size` only (cycles unknown) → passes
2. `verify_rtx` runs scripts, returns `verified.cycles`
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` is constructed at line 751
4. `submit_entry` is called at line 753 — **no second fee rate check against actual weight**

`submit_entry` performs RBF checks and time-relative re-verification on tip change, but never re-validates the fee rate against `get_transaction_weight(tx_size, verified.cycles)`. `TxEntry::fee_rate()` correctly uses the full weight for eviction/sorting, meaning the admitted entry will have a correctly computed (but below-minimum) fee rate once inside the pool.

## Impact Explanation
This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

- **Forced expensive verification**: Every admitted transaction triggers a full `ContextualTransactionVerifier` + script execution run. A transaction with `size=200, cycles=5,000,000` passes the size-based gate with `fee=200 shannons` (at `min_fee_rate=1000 shannons/KW`) but consumes 5M cycles of CPU. The actual weight is 852, giving an effective fee rate of ~234 shannons/KW — 4× below the minimum. An attacker can submit many such transactions to saturate the verify thread pool at a cost far below what the weight-based minimum would require.
- **Pool slot occupation**: Admitted entries remain in the pool until `limit_size` eviction triggers (only when the pool is full), temporarily occupying slots and affecting ancestor/descendant fee accounting.
- **Block template degradation**: Entries are scored by actual weight in `AncestorsScoreSortKey`, so they rank low, but their presence still affects pool state during the window before eviction.

## Likelihood Explanation
The attack is reachable by any unprivileged caller of the `send_transaction` RPC or any P2P peer. No special privileges are required. Crafting a transaction with a small serialized body and a loop-heavy lock/type script that consumes many cycles is straightforward. The attacker pays a real but below-minimum fee per transaction; the gap between size-based and weight-based cost grows with cycle count, making the bypass more severe for compute-intensive scripts. The attack is repeatable as long as the attacker has UTXOs to spend.

## Recommendation
After `verify_rtx` returns `verified.cycles` in `_process_tx`, perform a second fee rate check using the actual transaction weight before calling `submit_entry`:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(tx_pool_config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64())), snapshot));
}
```

This check belongs between lines 750 and 751 of `tx-pool/src/process.rs`, after `verified.cycles` is known and before `TxEntry::new` is constructed.

## Proof of Concept
1. Configure a CKB node with `tx_pool.min_fee_rate = 1000` shannons/KW.
2. Construct a transaction: `size ≈ 200 bytes`, lock script consuming ~5,000,000 cycles (tight RISC-V loop), fee = 200 shannons.
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200 shannons` → passes.
5. `verify_rtx` executes the script, consuming 5,000,000 cycles; actual weight = `max(200, 852) = 852`.
6. Entry is admitted with effective fee rate ≈ 234 shannons/KW, well below the 1000 shannons/KW minimum.
7. Repeat with many UTXOs to saturate the verify thread pool and occupy pool slots.

Code path: `send_transaction` → `submit_local_tx` → `_process_tx` → `pre_check`/`check_tx_fee` (size-only gate) → `verify_rtx` (cycles revealed) → `TxEntry::new` (no re-check) → `submit_entry`.

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
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

**File:** tx-pool/src/process.rs (L750-754)
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
