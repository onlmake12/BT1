Audit Report

## Title
Tx-Pool Promotes Transactions to `Proposed` Status Without Re-Verifying `since` Constraints After Reorg — (`tx-pool/src/pool.rs`, `tx-pool/src/process.rs`)

## Summary
During chain reorganization, `_update_tx_pool_for_reorg` promotes pending/gap transactions to `Proposed` status by calling `proposed_rtx`, which performs only a status label flip with no re-verification of `since` time-lock constraints against the new chain snapshot. A transaction whose `since` threshold is no longer satisfiable under the post-reorg tip can be silently promoted to `Proposed` and fed to the block assembler, which also skips `since` re-verification. The resulting block template contains an invalid transaction that the full contextual block verifier will reject, causing the miner to forfeit the block reward.

## Finding Description

**Root cause — `proposed_rtx` is a pure label flip:**

`proposed_rtx` (pool.rs:405-422) checks only for a duplicate status, then calls `set_entry_proposed`: [1](#0-0) 

`set_entry` (pool_map.rs:223-232) only mutates the `status` field in the multi-index map — no fee check, no capacity check, no `TimeRelativeTransactionVerifier` call: [2](#0-1) 

**Call site — `_update_tx_pool_for_reorg` (process.rs:1082-1094):**

The new post-reorg `snapshot` is available but never passed to any verifier before the status flip: [3](#0-2) 

**`remove_expired` does not cover `since` violations:**

The only cleanup after promotion is `remove_expired`, which filters only on wall-clock age (`expiry + entry.timestamp < now_ms`), not on `since` constraints: [4](#0-3) 

**Contrast with `readd_detached_tx` (process.rs:878-914):**

Transactions from *detached blocks* go through the full pipeline — `resolve_tx` → `check_tx_fee` → `verify_rtx` (which calls `TimeRelativeTransactionVerifier`). Transactions already *in the pool* that are merely promoted during the same reorg receive none of this re-verification: [5](#0-4) 

**Block assembler does not re-verify `since`:**

`calc_dao` only calls `entry.rtx.check(...)`, which is a cell-resolution (double-spend/dead-cell) check. No `TimeRelativeTransactionVerifier` is invoked: [6](#0-5) 

**Full block verifier does catch the violation — but only after the block is submitted:**

`TransactionVerifier::verify` in the contextual block verifier creates `TxVerifyEnv::new_commit` and runs `TimeRelativeTransactionVerifier`, so the assembled block is rejected by the network: [7](#0-6) 

**Concrete valid scenario (corrected from claim):**

- `since = AbsoluteBlockNumber(100)`, proposal window closest = 2.
- T submitted at tip height 97: check `97 + 1 + 2 = 100 ≥ 100` passes; T enters `Pending`.
- Block 98 proposes T; `_update_tx_pool_for_reorg` promotes T to `Proposed` at tip 98. (Valid: earliest commit = 98 + 2 = 100 ≥ 100.)
- Reorg arrives; new tip is block 96 (2-block reorg). `remove_by_detached_proposal` moves T back to `Pending`.
- Miner mines block 97, re-proposing T. `_update_tx_pool_for_reorg` is called with tip = 97; `snapshot.proposals().contains_proposed(&T)` is true; `proposed_rtx` promotes T to `Proposed` — **no `since` check**.
- Block assembler packages T into block 98 (tip 97 + 1). Full verifier: `TxVerifyEnv::new_commit` at block 98; `98 < 100` → `Immature` error → block rejected.
- T remains in `Proposed` status; every subsequent block template includes T; every block is rejected until T is manually evicted.

The `remove_by_detached_proposal` path confirms T is moved back to `Pending` without re-verification: [8](#0-7) 

## Impact Explanation

A miner operating in `mine_mode` with a `since`-constrained transaction in the pool will repeatedly produce block templates containing the invalid transaction. Each submitted block is rejected by the network's contextual block verifier, causing the miner to forfeit the block reward for every round until the invalid transaction is manually removed. DAO withdrawal transactions, which are common on CKB mainnet, use epoch-based `since` fields and are directly affected. This constitutes concrete economic damage to miners — matching the allowed impact class: **Vulnerabilities which could easily damage CKB economy**.

## Likelihood Explanation

- A 2-block reorg is sufficient to trigger the condition; reorgs of this depth occur naturally on mainnet without any attacker.
- DAO withdrawal transactions with `since` epoch constraints are among the most common time-locked transactions on CKB.
- No special privileges or majority hashpower are required; a natural network reorg is the entry point.
- Once triggered, the condition is self-sustaining: the invalid transaction stays in `Proposed` and is included in every subsequent block template until manually removed via `remove_transaction` RPC.

## Recommendation

In `_update_tx_pool_for_reorg`, before calling `proposed_rtx` or `gap_rtx`, run `time_relative_verify` (or the full `verify_rtx`) against the new snapshot for each candidate transaction. If verification fails, call `callbacks.call_reject` and remove the entry instead of promoting it. This mirrors the pattern already used in `readd_detached_tx` (process.rs:888-912): [5](#0-4) 

The `time_relative_verify` helper is already available in `tx-pool/src/util.rs`: [9](#0-8) 

## Proof of Concept

1. Configure a CKB node in mine mode (`block_assembler` enabled).
2. Submit a transaction T with `since = AbsoluteBlockNumber(N)` where N = current_tip + 3 (so it is accepted into the pool).
3. Mine one block (block N-2) that proposes T; mine one more blank block (N-1) so T enters `Proposed` status. Confirm via `get_raw_tx_pool`.
4. Inject a competing fork rooted 2 blocks back (tip drops to N-4). T returns to `Pending`.
5. Mine block N-3 on the new fork, re-proposing T. Observe via `get_raw_tx_pool` that T is back in `Proposed` status even though `tip_height + 1 < N`.
6. Call `get_block_template` — T appears in `transactions`.
7. Submit the assembled block; the network rejects it with `TransactionFailedToVerify: Immature`.
8. Repeat step 6-7 to confirm the condition persists until T is manually removed.

### Citations

**File:** tx-pool/src/pool.rs (L271-288)
```rust
    pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
        let now_ms = ckb_systemtime::unix_time_as_millis();

        let removed: Vec<_> = self
            .pool_map
            .iter()
            .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
            .map(|entry| entry.inner.clone())
            .collect();

        for entry in removed {
            let tx_hash = entry.transaction().hash();
            debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
            self.pool_map.remove_entry(&entry.proposal_short_id());
            let reject = Reject::Expiry(entry.timestamp);
            callbacks.call_reject(self, &entry, reject);
        }
    }
```

**File:** tx-pool/src/pool.rs (L333-356)
```rust
    pub(crate) fn remove_by_detached_proposal<'a>(
        &mut self,
        ids: impl Iterator<Item = &'a ProposalShortId>,
    ) {
        for id in ids {
            if let Some(e) = self.pool_map.get_by_id(id) {
                let status = e.status;
                if status == Status::Pending {
                    continue;
                }
                let mut entries = self.pool_map.remove_entry_and_descendants(id);
                entries.sort_unstable_by_key(|entry| entry.ancestors_count);
                for mut entry in entries {
                    let tx_hash = entry.transaction().hash();
                    entry.reset_statistic_state();
                    let ret = self.add_pending(entry);
                    debug!(
                        "remove_by_detached_proposal from {:?} {} add_pending {:?}",
                        status, tx_hash, ret
                    );
                }
            }
        }
    }
```

**File:** tx-pool/src/pool.rs (L405-422)
```rust
    pub(crate) fn proposed_rtx(&mut self, short_id: &ProposalShortId) -> Result<(), Reject> {
        match self.get_pool_entry(short_id) {
            Some(entry) => {
                let tx_hash = entry.inner.transaction().hash();
                if entry.status == Status::Proposed {
                    Err(Reject::Duplicated(tx_hash))
                } else {
                    debug!("proposed_rtx: {:?} => {:?}", tx_hash, short_id);
                    self.set_entry_proposed(short_id);
                    Ok(())
                }
            }
            None => Err(Reject::Malformed(
                String::from("invalid short_id"),
                Default::default(),
            )),
        }
    }
```

**File:** tx-pool/src/component/pool_map.rs (L223-233)
```rust
    /// Change the status of the entry, only used for `gap_rtx` and `proposed_rtx`
    pub(crate) fn set_entry(&mut self, short_id: &ProposalShortId, status: Status) {
        let mut old_status = None;
        self.entries
            .modify_by_id(short_id, |e| {
                old_status = Some(e.status);
                e.status = status;
            })
            .expect("inconsistent pool");
        self.track_entry_statics(old_status, Some(status));
    }
```

**File:** tx-pool/src/process.rs (L888-912)
```rust
            if let Ok((rtx, status)) = resolve_tx(tx_pool, tx_pool.snapshot(), tx, false)
                && let Ok(fee) = check_tx_fee(tx_pool, tx_pool.snapshot(), &rtx, tx_size)
            {
                let verify_cache = fetched_cache.get(&tx_hash).cloned();
                let snapshot = tx_pool.cloned_snapshot();
                let tip_header = snapshot.tip_header();
                let tx_env = Arc::new(status.with_env(tip_header));
                if let Ok(verified) = verify_rtx(
                    snapshot,
                    Arc::clone(&rtx),
                    tx_env,
                    &verify_cache,
                    max_cycles,
                    None,
                )
                .await
                {
                    let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
                    if let Err(e) = _submit_entry(tx_pool, status, entry, &self.callbacks) {
                        error!("readd_detached_tx submit_entry {} error {}", tx_hash, e);
                    } else {
                        debug!("readd_detached_tx submit_entry {}", tx_hash);
                    }
                }
            }
```

**File:** tx-pool/src/process.rs (L1082-1094)
```rust
        for (id, entry) in proposals {
            debug!("begin to proposed: {:x}", id);
            if let Err(e) = tx_pool.proposed_rtx(&id) {
                debug!(
                    "Failed to add proposed tx {}, reason: {}",
                    entry.transaction().hash(),
                    e
                );
                callbacks.call_reject(tx_pool, &entry, e);
            } else {
                callbacks.call_proposed(&entry)
            }
        }
```

**File:** tx-pool/src/block_assembler/mod.rs (L640-668)
```rust
        let checked_entries: Vec<_> = block_in_place(|| {
            entries
                .into_iter()
                .filter_map(|entry| {
                    let overlay_cell_checker =
                        OverlayCellChecker::new(&transactions_checker, snapshot);
                    if let Err(err) =
                        entry
                            .rtx
                            .check(&mut seen_inputs, &overlay_cell_checker, snapshot)
                    {
                        error!(
                            "Resolving transactions while building block template, \
                             tip_number: {}, tip_hash: {}, tx_hash: {}, error: {:?}",
                            tip_header.number(),
                            tip_header.hash(),
                            entry.transaction().hash(),
                            err
                        );
                        // Returning the out_point makes debugging easier and provides better logs.
                        checked_failed_txs
                            .push((entry.proposal_short_id(), err.out_point().cloned()));
                        None
                    } else {
                        transactions_checker.insert(entry.transaction());
                        Some(entry)
                    }
                })
                .collect()
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L400-424)
```rust
        let tx_env = Arc::new(TxVerifyEnv::new_commit(&self.header));

        // make verifiers orthogonal
        let ret = resolved
            .par_iter()
            .enumerate()
            .map(|(index, tx)| {
                let wtx_hash = tx.transaction.witness_hash();

                if let Some(completed) = fetched_cache.get(&wtx_hash) {
                    TimeRelativeTransactionVerifier::new(
                            Arc::clone(tx),
                            Arc::clone(&self.context.consensus),
                            self.context.store.as_data_loader(),
                            Arc::clone(&tx_env),
                        )
                        .verify()
                        .map_err(|error| {
                            BlockTransactionsError {
                                index: index as u32,
                                error,
                            }
                            .into()
                        })
                        .map(|_| (wtx_hash, *completed))
```

**File:** tx-pool/src/util.rs (L134-148)
```rust
pub(crate) fn time_relative_verify(
    snapshot: Arc<Snapshot>,
    rtx: Arc<ResolvedTransaction>,
    tx_env: TxVerifyEnv,
) -> Result<(), Reject> {
    let consensus = snapshot.cloned_consensus();
    TimeRelativeTransactionVerifier::new(
        rtx,
        consensus,
        snapshot.as_data_loader(),
        Arc::new(tx_env),
    )
    .verify()
    .map_err(Reject::Verification)
}
```
