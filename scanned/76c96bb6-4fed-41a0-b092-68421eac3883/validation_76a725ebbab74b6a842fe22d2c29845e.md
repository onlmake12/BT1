### Title
Silent Transaction Loss in `remove_by_detached_proposal` During Reorg — (File: `tx-pool/src/pool.rs`)

---

### Summary

During a chain reorganization, `remove_by_detached_proposal` removes transactions from the `Gap`/`Proposed` state and attempts to re-add them as `Pending`. If `add_pending` fails, the transactions are **permanently dropped from the pool** with only a debug-level log and no recovery mechanism — a direct structural analog to the `sendNFTs` silent-catch pattern.

---

### Finding Description

In `remove_by_detached_proposal` (`tx-pool/src/pool.rs`, lines 333–356), when a proposal is detached during a reorg, the following sequence occurs:

1. All entries matching the detached proposal ID (and their descendants) are **unconditionally removed** from the pool via `remove_entry_and_descendants` (line 343).
2. Each removed entry is then re-added as `Pending` via `add_pending` (line 348).
3. If `add_pending` returns `Err(...)`, the result is only logged at **debug level** (lines 349–352) — not `warn` or `error`.
4. The transaction is **permanently gone from the pool** with no rollback, no re-queue, and no notification to the submitter.

```rust
// tx-pool/src/pool.rs:343-353
let mut entries = self.pool_map.remove_entry_and_descendants(id); // state mutated
entries.sort_unstable_by_key(|entry| entry.ancestors_count);
for mut entry in entries {
    let tx_hash = entry.transaction().hash();
    entry.reset_statistic_state();
    let ret = self.add_pending(entry);          // may fail
    debug!(                                     // failure only at debug level
        "remove_by_detached_proposal from {:?} {} add_pending {:?}",
        status, tx_hash, ret
    );
    // no else branch, no recovery, no error log
}
``` [1](#0-0) 

`add_pending` delegates to `pool_map.add_entry(entry, Status::Pending)`: [2](#0-1) 

`add_entry` can return `Reject` errors including `ExceededMaximumAncestorsCount`. After a reorg that detaches a block, previously committed ancestor transactions are restored to the pool, which can cause a descendant transaction's ancestor count to exceed `max_ancestors_count` — a condition that did not exist before the reorg. In that case, `add_pending` returns `Err`, the entry is silently discarded, and the pool is left in a state where the transaction no longer exists despite being valid.

This function is called from `_update_tx_pool_for_reorg`: [3](#0-2) 

Which is invoked from `update_tx_pool_for_reorg` after every new best block: [4](#0-3) 

---

### Impact Explanation

Any transaction in `Gap` or `Proposed` status at the time of a reorg can be permanently evicted from the tx pool without any error surfaced to the operator or the submitter. The transaction's inputs (cells) remain spendable, so funds are not lost, but:

- The transaction is silently gone from the pool — the submitter receives no rejection signal.
- A miner node loses the fee revenue from that transaction.
- The submitter must independently detect the loss and resubmit, with no protocol-level indication that resubmission is needed.
- In a chain of dependent transactions, if an ancestor is silently dropped, all descendants also become unsubmittable until the ancestor is resubmitted.

**Impact: Medium** — tx-pool consistency is broken silently; no fund loss, but reliable transaction propagation is degraded.

---

### Likelihood Explanation

Reorgs are a normal, externally triggerable network event: any peer can cause a reorg by relaying a valid longer chain. The `add_pending` failure specifically requires that a detached block's re-surfaced ancestor transactions push a descendant over `max_ancestors_count`. This is a realistic scenario on a loaded node during a multi-block reorg. No privileged access is required.

**Likelihood: Medium** — reorgs are common; the ancestor-count overflow condition requires a moderately deep reorg or a heavily chained transaction set, both of which occur in practice.

---

### Recommendation

In `remove_by_detached_proposal`, if `add_pending` returns `Err`, the failure should be logged at `error` or `warn` level (not `debug`), and the transaction hash should be recorded so the submitter or operator can detect the loss. Ideally, a fallback mechanism should attempt to notify the original submitter (via the relay channel) that the transaction must be resubmitted, mirroring the pattern used in `after_process` for other rejection paths: [5](#0-4) 

---

### Proof of Concept

1. Submit a chain of `N` transactions (where `N` is near `max_ancestors_count`) to a node.
2. Wait for the tip block to propose the root transaction (moving it to `Proposed` or `Gap`).
3. Trigger a reorg of depth ≥ 1 by relaying a valid competing chain (no special privileges needed — any peer can do this).
4. The reorg calls `_update_tx_pool_for_reorg` → `remove_by_detached_proposal`.
5. The root transaction is removed from `Proposed`/`Gap` and re-added as `Pending` — but now its previously-committed ancestors are also back in the pool, pushing the descendant chain's ancestor count over the limit.
6. `add_pending` returns `Err(Reject::ExceededMaximumAncestorsCount)`.
7. The transaction is permanently dropped from the pool. Only a debug log is emitted. The submitter receives no signal. [6](#0-5) [7](#0-6)

### Citations

**File:** tx-pool/src/pool.rs (L131-136)
```rust
    pub(crate) fn add_pending(
        &mut self,
        entry: TxEntry,
    ) -> Result<(bool, HashSet<TxEntry>), Reject> {
        self.pool_map.add_entry(entry, Status::Pending)
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

**File:** tx-pool/src/process.rs (L502-526)
```rust
                Err(reject) => {
                    debug!(
                        "after_process {} {} remote reject: {} ",
                        tx_hash, peer, reject
                    );
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
                    } else {
                        if reject.is_malformed_tx() {
                            self.ban_malformed(peer, format!("reject {reject}")).await;
                        }
                        if reject.is_allowed_relay() {
                            self.send_result_to_relayer(TxVerificationResult::Reject {
                                tx_hash: tx_hash.clone(),
                            });
                        }
                        if reject.should_recorded() {
                            self.put_recent_reject(&tx_hash, reject).await;
                        }
                    }
                }
```

**File:** tx-pool/src/process.rs (L1039-1056)
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
```
