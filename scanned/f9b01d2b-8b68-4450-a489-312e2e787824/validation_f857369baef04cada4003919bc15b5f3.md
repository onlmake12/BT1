### Title
Tx-Pool Promotes Transactions to `Proposed` Status Without Re-Verifying Time-Relative (`since`) Constraints After Reorg — (`tx-pool/src/pool.rs`, `tx-pool/src/process.rs`)

---

### Summary

During chain reorganization, `_update_tx_pool_for_reorg` promotes pending/gap transactions to `Proposed` (or `Gap`) status by calling `proposed_rtx` / `gap_rtx`. These functions only mutate the status label in the pool map — they perform **no re-verification** of the transaction's validity against the new chain snapshot. After a reorg that lowers the chain tip height or changes the epoch, a transaction whose `since` time-lock constraint was satisfied before the reorg may no longer be satisfiable, yet it is silently promoted to `Proposed` and handed to the block assembler as if it were commit-ready.

---

### Finding Description

**Root cause — `proposed_rtx` / `gap_rtx` skip all validity checks:**

`proposed_rtx` (pool.rs:405-422) checks only for a duplicate status and then calls `set_entry_proposed`:

```rust
pub(crate) fn proposed_rtx(&mut self, short_id: &ProposalShortId) -> Result<(), Reject> {
    match self.get_pool_entry(short_id) {
        Some(entry) => {
            let tx_hash = entry.inner.transaction().hash();
            if entry.status == Status::Proposed {
                Err(Reject::Duplicated(tx_hash))
            } else {
                self.set_entry_proposed(short_id);   // ← only a label flip
                Ok(())
            }
        }
        ...
    }
}
```

`set_entry_proposed` calls `pool_map.set_entry(short_id, Status::Proposed)` (pool_map.rs:224-232), which only modifies the `status` field in the multi-index map — no fee check, no capacity check, no `TimeRelativeTransactionVerifier` call.

**Call site — `_update_tx_pool_for_reorg` (process.rs:1082-1106):**

```rust
for (id, entry) in proposals {
    if let Err(e) = tx_pool.proposed_rtx(&id) {
        callbacks.call_reject(tx_pool, &entry, e);
    } else {
        callbacks.call_proposed(&entry)   // ← promoted with no re-verify
    }
}
```

The new `snapshot` (with the post-reorg tip header) is available at this point but is never passed to any verifier before the status flip.

**Contrast with `readd_detached_tx` (process.rs:878-914):**

Transactions from *detached blocks* that are re-added to the pool go through the full pipeline: `resolve_tx` → `check_tx_fee` → `verify_rtx` (which calls `TimeRelativeTransactionVerifier`). Transactions already *in the pool* that are merely promoted during the same reorg receive none of this re-verification.

**Concrete inconsistency scenario:**

1. Transaction T has `since = AbsoluteBlockNumber(100)` (input must not be spent before block 100).
2. Chain tip is at block 105; T is in `Pending` status.
3. A new block at height 106 proposes T → `_update_tx_pool_for_reorg` calls `proposed_rtx`, promoting T to `Proposed`. At this point the tip is 106 ≥ 100, so T *would* be valid — but no check is actually performed.
4. A reorg arrives (peer sends a longer fork rooted at block 95). The new tip is block 97.
5. T's proposal is detached; `remove_by_detached_proposal` moves T back to `Pending`.
6. The next block at height 98 re-proposes T. `_update_tx_pool_for_reorg` calls `proposed_rtx` again, promoting T to `Proposed` — **without checking that 98 < 100**.
7. T now sits in the `Proposed` pool as commit-ready, but its `since` constraint is violated under the current tip.

---

### Impact Explanation

The block assembler (`TxSelector`) draws directly from the `Proposed` pool and does not re-run `TimeRelativeTransactionVerifier` before assembling a candidate block. A miner operating in `mine_mode` will include T in the next block template. When that block is submitted for full contextual verification, the `since` constraint failure causes the block to be rejected by the network. The miner forfeits the block reward for that round. Additionally, the invalid transaction occupies the `Proposed` pool and may be relayed to peers as a valid, commit-ready transaction.

---

### Likelihood Explanation

- Reorgs are a normal, unprivileged network event — any peer can trigger one by relaying a longer valid chain.
- Transactions with `since` time-locks (absolute block number, epoch, or timestamp) are common in CKB (e.g., DAO withdrawal transactions use epoch-based `since` fields).
- A reorg that reduces the tip height by even one block is sufficient if a transaction's `since` threshold falls in the gap.
- No special privileges, keys, or majority hashpower are required; an ordinary sync peer delivering a longer fork is the entry point.

---

### Recommendation

In `_update_tx_pool_for_reorg`, before calling `proposed_rtx` or `gap_rtx`, run `TimeRelativeTransactionVerifier` (or the full `ContextualTransactionVerifier`) against the new snapshot for each candidate transaction. If verification fails, call `callbacks.call_reject` and remove the entry instead of promoting it. This mirrors the pattern already used in `readd_detached_tx` (process.rs:888-912), which correctly re-verifies before re-admitting detached transactions.

---

### Proof of Concept

1. Configure a CKB node in mine mode (`block_assembler` enabled).
2. Submit a transaction T with `since = AbsoluteBlockNumber(N)` where N is slightly above the current tip.
3. Mine blocks until T is proposed and promoted to `Proposed` status (confirm via `get_raw_tx_pool` RPC).
4. Inject a competing fork (via a second node) that reorgs the chain to a tip height below N, then re-proposes T in the new fork.
5. Observe via `get_raw_tx_pool` that T is back in `Proposed` status even though `tip_height < N`.
6. Call `get_block_template` — T appears in the template's `transactions` list.
7. Submit the assembled block; the network rejects it with a `since` constraint violation.

**Key code references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** tx-pool/src/process.rs (L878-913)
```rust
    async fn readd_detached_tx(
        &self,
        tx_pool: &mut TxPool,
        txs: Vec<TransactionView>,
        fetched_cache: HashMap<Byte32, CacheEntry>,
    ) {
        let max_cycles = self.tx_pool_config.max_tx_verify_cycles;
        for tx in txs {
            let tx_size = tx.data().serialized_size_in_block();
            let tx_hash = tx.hash();
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
        }
```

**File:** tx-pool/src/process.rs (L1039-1114)
```rust
fn _update_tx_pool_for_reorg(
    tx_pool: &mut TxPool,
    attached: &LinkedHashSet<TransactionView>,
    detached_headers: &HashSet<Byte32>,
    detached_proposal_id: HashSet<ProposalShortId>,
    snapshot: Arc<Snapshot>,
    callbacks: &Callbacks,
    mine_mode: bool,
) {
    tx_pool.snapshot = Arc::clone(&snapshot);

    // NOTE: `remove_by_detached_proposal` will try to re-put the given expired/detached proposals into
    // pending-pool if they can be found within txpool. As for a transaction
    // which is both expired and committed at the one time(commit at its end of commit-window),
    // we should treat it as a committed and not re-put into pending-pool. So we should ensure
    // that involves `remove_committed_txs` before `remove_expired`.
    tx_pool.remove_committed_txs(attached.iter(), callbacks, detached_headers);
    tx_pool.remove_by_detached_proposal(detached_proposal_id.iter());

    // mine mode:
    // pending ---> gap ----> proposed
    // try move gap to proposed
    if mine_mode {
        let mut proposals = Vec::new();
        let mut gaps = Vec::new();

        for entry in tx_pool.pool_map.entries.get_by_status(&Status::Gap) {
            let short_id = entry.inner.proposal_short_id();
            if snapshot.proposals().contains_proposed(&short_id) {
                proposals.push((short_id, entry.inner.clone()));
            }
        }

        for entry in tx_pool.pool_map.entries.get_by_status(&Status::Pending) {
            let short_id = entry.inner.proposal_short_id();
            let elem = (short_id.clone(), entry.inner.clone());
            if snapshot.proposals().contains_proposed(&short_id) {
                proposals.push(elem);
            } else if snapshot.proposals().contains_gap(&short_id) {
                gaps.push(elem);
            }
        }

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

        for (id, entry) in gaps {
            debug!("begin to gap: {:x}", id);
            if let Err(e) = tx_pool.gap_rtx(&id) {
                debug!(
                    "Failed to add tx to gap {}, reason: {}",
                    entry.transaction().hash(),
                    e
                );
                callbacks.call_reject(tx_pool, &entry, e.clone());
            }
        }
    }

    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
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

**File:** tx-pool/src/component/pool_map.rs (L223-232)
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
```
