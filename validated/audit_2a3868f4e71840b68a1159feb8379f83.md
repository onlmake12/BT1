### Title
Gap-Status Transactions Never Reverted to Pending After Reorg Detaches Their Proposal — (`tx-pool/src/process.rs`, `util/proposal-table/src/lib.rs`)

---

### Summary

During a chain reorganization, `_update_tx_pool_for_reorg` computes `detached_proposal_id` exclusively from proposals that left the **proposed** (`set`) window. Proposals that leave the **gap** window are never included. As a result, transactions in `Status::Gap` whose on-chain proposal was detached are never moved back to `Status::Pending`. They remain frozen in `Gap` status — unable to be re-proposed or committed — until the timestamp-based expiry fires.

---

### Finding Description

The CKB two-step transaction confirmation protocol defines three in-pool states: `Pending`, `Gap`, and `Proposed`. [1](#0-0) 

When a block is mined that contains a transaction's `ProposalShortId`, the transaction moves from `Pending` → `Gap`. When enough blocks pass (the proposal window's `closest` distance), it moves `Gap` → `Proposed` and becomes committable.

During a reorg, `ProposalTable::finalize` computes the set of proposal IDs to hand back to the tx-pool for demotion:

```rust
let removed_ids: HashSet<ProposalShortId> =
    origin.set().difference(&new_ids).cloned().collect();
``` [2](#0-1) 

`origin.set()` is the **old proposed window** (`set`). `new_ids` is the **new proposed window**. The difference captures only proposals that left the commit-eligible window. **Proposals that were in `origin.gap` (the gap window) and are no longer in the new gap window are never included in `removed_ids`.**

This `removed_ids` becomes `detached_proposal_id`, which is passed to `remove_by_detached_proposal`: [3](#0-2) 

`remove_by_detached_proposal` only iterates over the IDs it receives. A `Gap`-status transaction whose proposing block was detached is never in that set, so it is never moved back to `Pending`: [4](#0-3) 

In `mine_mode`, the reorg handler does scan `Gap` entries, but only to promote them to `Proposed` — there is no branch to demote them back to `Pending` when their proposal is no longer in any window: [5](#0-4) 

In non-mine mode, `Gap` entries are not touched at all during reorg.

---

### Impact Explanation

A transaction T stuck in `Status::Gap` after its proposal is detached:

1. **Cannot be committed** — it is not in the proposed window.
2. **Cannot be re-proposed** — the block assembler's `package_proposals` draws from `Pending` entries; a `Gap` entry is invisible to it.
3. **Remains in the pool in an inconsistent state** — the pool reports it as "pending" to RPC callers (since `get_all_entry_info` groups `Gap` with `Pending`), but it will never advance.
4. **Eventually expires** — `remove_expired` iterates all entries including `Gap`, so the transaction is eventually evicted by timestamp. Until then, the transaction is silently stalled. [6](#0-5) 

The impact is **medium**: no direct fund loss, but transactions are silently delayed or dropped after a reorg, degrading reliability for users and applications that depend on timely confirmation.

---

### Likelihood Explanation

- Reorgs are a normal part of PoW chain operation and occur on mainnet without any attacker involvement.
- Any peer that relays a competing chain of equal or greater difficulty can trigger a reorg.
- The condition is met whenever a reorg detaches a block that contained a proposal for a transaction currently in `Status::Gap` — a routine scenario during natural forks near the chain tip.
- No privileged access, key material, or majority hashpower is required.

---

### Recommendation

In `ProposalTable::finalize`, also compute and return the set of proposals that left the **gap** window:

```rust
let removed_gap_ids: HashSet<ProposalShortId> =
    origin.gap().difference(&gap).cloned().collect();
```

Pass this set to a new or extended handler in `_update_tx_pool_for_reorg` that moves `Gap`-status entries whose proposals are no longer in any window back to `Pending` (mirroring the existing `remove_by_detached_proposal` logic).

Alternatively, in the `mine_mode` block that iterates `Status::Gap` entries, add an `else` branch that demotes entries to `Pending` when `!snapshot.proposals().contains_proposed(&short_id) && !snapshot.proposals().contains_gap(&short_id)`. [7](#0-6) 

---

### Proof of Concept

**Setup:** Default mainnet proposal window (`closest = 2`, `farthest = 10`).

1. Submit transaction T to the tx-pool → `Status::Pending`.
2. Mine block B1 at height H that includes T's `ProposalShortId` in its proposals. T moves to `Status::Gap`.
3. A competing chain of length ≥ 2 arrives, detaching B1. The new tip is at height H (different block) or H-1.
4. `update_proposal_table` removes B1's proposals from the `ProposalTable`.
5. `finalize` is called. T's short_id was in `origin.gap`, not `origin.set`. `removed_ids = origin.set() - new_ids` does not contain T's short_id.
6. `remove_by_detached_proposal(detached_proposal_id.iter())` is called — T is not processed.
7. In mine_mode: the Gap-scan loop checks `contains_proposed(T)` → false; T is not moved to Proposed and not moved back to Pending.
8. T remains in `Status::Gap`. The block assembler never re-proposes T. T is confirmed only after its timestamp expiry evicts it, forcing the user to resubmit. [8](#0-7) [9](#0-8)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L23-28)
```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Status {
    Pending,
    Gap,
    Proposed,
}
```

**File:** util/proposal-table/src/lib.rs (L93-157)
```rust
    pub fn finalize(
        &mut self,
        origin: &ProposalView,
        number: BlockNumber,
    ) -> (HashSet<ProposalShortId>, ProposalView) {
        let candidate_number = number + 1;
        let proposal_start = candidate_number.saturating_sub(self.proposal_window.farthest());
        let proposal_end = candidate_number.saturating_sub(self.proposal_window.closest());

        if proposal_start > 1 {
            self.table = self.table.split_off(&proposal_start);
        }

        ckb_logger::trace!("[proposal_finalize] table {:?}", self.table);

        // - if candidate_number <= self.proposal_window.closest()
        //      new_ids = []
        //      gap = [1..candidate_number]
        // - else
        //      new_ids = [candidate_number- farthest..= candidate_number- closest]
        //      gap = [candidate_number- closest + 1..candidate_number]
        // - end
        let (new_ids, gap) = if candidate_number <= self.proposal_window.closest() {
            (
                HashSet::new(),
                self.table
                    .range((Bound::Unbounded, Bound::Included(&number)))
                    .flat_map(|pair| pair.1)
                    .cloned()
                    .collect(),
            )
        } else {
            (
                self.table
                    .range((
                        Bound::Included(&proposal_start),
                        Bound::Included(&proposal_end),
                    ))
                    .flat_map(|pair| pair.1)
                    .cloned()
                    .collect(),
                self.table
                    .range((Bound::Excluded(&proposal_end), Bound::Included(&number)))
                    .flat_map(|pair| pair.1)
                    .cloned()
                    .collect(),
            )
        };

        let removed_ids: HashSet<ProposalShortId> =
            origin.set().difference(&new_ids).cloned().collect();
        ckb_logger::trace!(
            "[proposal_finalize] number {} proposal_start {}----proposal_end {}",
            number,
            proposal_start,
            proposal_end
        );
        ckb_logger::trace!(
            "[proposal_finalize] number {} new_ids {:?}----removed_ids {:?}",
            number,
            new_ids,
            removed_ids
        );
        (removed_ids, ProposalView::new(gap, new_ids))
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

**File:** tx-pool/src/pool.rs (L270-287)
```rust
    // Expire all transaction (and their dependencies) in the pool.
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
```

**File:** tx-pool/src/pool.rs (L331-356)
```rust
    // remove transaction with detached proposal from gap and proposed
    // try re-put to pending
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
