Audit Report

## Title
Gap-status tx-pool entries not demoted to Pending after reorgs that remove proposals from the gap window — (`util/proposal-table/src/lib.rs`, `tx-pool/src/process.rs`)

## Summary
`ProposalTable::finalize` computes `removed_ids` exclusively as `origin.set().difference(&new_ids)`, covering only proposals leaving the commit window. Proposals that were exclusively in `origin.gap()` and fall out of the gap window during a reorg are never included in the returned `detached_proposal_id`. Consequently, `_update_tx_pool_for_reorg` never calls `remove_by_detached_proposal` for them, leaving those transactions permanently stuck in `Status::Gap` until pool eviction pressure forces removal.

## Finding Description
In `util/proposal-table/src/lib.rs`, `finalize` computes:

```rust
let removed_ids: HashSet<ProposalShortId> =
    origin.set().difference(&new_ids).cloned().collect();
``` [1](#0-0) 

There is no corresponding `origin.gap().difference(&new_gap)` computation. The function returns only set-level diffs.

In `chain/src/verify.rs`, the returned `detached_proposal_id` is stored in `fork.detached_proposal_id` and forwarded to `update_tx_pool_for_reorg`: [2](#0-1) 

In `_update_tx_pool_for_reorg`, `remove_by_detached_proposal` is called only with this set-level diff: [3](#0-2) 

`remove_by_detached_proposal` correctly demotes Gap/Proposed→Pending, but it is never invoked for gap-only proposals: [4](#0-3) 

The mine_mode loop only promotes Gap→Proposed; it has no demotion path for Gap→Pending when a proposal leaves the gap: [5](#0-4) 

`remove_expired` is explicitly for pending entries only: [6](#0-5) 

The only safety net is `limit_size`, which evicts Gap entries only under pool size pressure, not proactively on reorg: [7](#0-6) 

**Concrete scenario with `ProposalWindow(2, 10)`:**
- Tip N: proposal P at block N → in `gap` (block N is within `(N-1, N]`).
- Reorg to tip N-1: gap window shifts to `(N-2, N-1]`. Block N is outside the new gap.
- `origin.set()` at tip N does not contain P (P was only in gap, not set).
- `removed_ids = origin.set().difference(&new_ids)` → P absent → `removed_ids` is empty.
- `remove_by_detached_proposal` is never called for P.
- P remains `Status::Gap` in the tx-pool indefinitely.

## Impact Explanation
Stale Gap-status entries accumulate in the tx-pool after reorgs. They cannot be committed (not in the proposed set) and are not re-proposed (not in Pending). In mine_mode, miners will not re-propose these transactions, making them invisible to block assembly. The pool's view of which transactions are "gap-proposed" diverges from the canonical chain's `ProposalView`. This matches the allowed impact: **Medium (2001–10000 points) — Suboptimal implementation of CKB state storage mechanism**.

## Likelihood Explanation
Any unprivileged peer can trigger this by relaying a valid competing chain that causes a reorg rolling the tip back by even one block — a normal P2P operation requiring no special privileges or majority hashpower. The condition is easily and repeatedly triggerable.

## Recommendation
In `finalize` (`util/proposal-table/src/lib.rs`), also compute and return the gap-level diff:
```rust
let removed_gap_ids: HashSet<ProposalShortId> =
    origin.gap().difference(&gap).cloned().collect();
```
Return both sets (or union them into `removed_ids`). In `_update_tx_pool_for_reorg` (`tx-pool/src/process.rs`), call `remove_by_detached_proposal` for both the set-level and gap-level diffs, so Gap-status entries whose proposals are no longer in the current gap window are properly demoted to Pending.

## Proof of Concept
```rust
// ProposalWindow(2, 10): closest=2, farthest=10
let window = ProposalWindow(2, 10);
let mut table = ProposalTable::new(window);
let p = ProposalShortId::new([1u8; 10]);
table.insert(5, iter::once(p.clone()).collect()); // P proposed at block 5

// finalize to tip 5: P enters gap
let (_, view_at_5) = table.finalize(&ProposalView::default(), 5);
assert!(view_at_5.contains_gap(&p));
assert!(!view_at_5.contains_proposed(&p));

// reorg: finalize to tip 4: P should leave gap (block 5 > tip 4)
let (removed_ids, view_at_4) = table.finalize(&view_at_5, 4);
assert!(!view_at_4.contains_gap(&p));
assert!(!view_at_4.contains_proposed(&p));
// BUG: removed_ids is empty — P was only in origin.gap(), not origin.set()
assert!(removed_ids.is_empty()); // passes — P is never reported as detached
// remove_by_detached_proposal is never called for P;
// tx-pool retains P as Status::Gap indefinitely.
```

### Citations

**File:** util/proposal-table/src/lib.rs (L142-143)
```rust
        let removed_ids: HashSet<ProposalShortId> =
            origin.set().difference(&new_ids).cloned().collect();
```

**File:** chain/src/verify.rs (L374-391)
```rust
            let (detached_proposal_id, new_proposals) = self
                .proposal_table
                .finalize(origin_proposals, tip_header.number());
            fork.detached_proposal_id = detached_proposal_id;

            let new_snapshot =
                self.shared
                    .new_snapshot(tip_header, cannon_total_difficulty, epoch, new_proposals);

            self.shared.store_snapshot(Arc::clone(&new_snapshot));

            let tx_pool_controller = self.shared.tx_pool_controller();
            if tx_pool_controller.service_started() {
                if let Err(e) = tx_pool_controller.update_tx_pool_for_reorg(
                    fork.detached_blocks().clone(),
                    fork.attached_blocks().clone(),
                    fork.detached_proposal_id().clone(),
                    new_snapshot,
```

**File:** tx-pool/src/process.rs (L1055-1056)
```rust
    tx_pool.remove_committed_txs(attached.iter(), callbacks, detached_headers);
    tx_pool.remove_by_detached_proposal(detached_proposal_id.iter());
```

**File:** tx-pool/src/process.rs (L1065-1070)
```rust
        for entry in tx_pool.pool_map.entries.get_by_status(&Status::Gap) {
            let short_id = entry.inner.proposal_short_id();
            if snapshot.proposals().contains_proposed(&short_id) {
                proposals.push((short_id, entry.inner.clone()));
            }
        }
```

**File:** tx-pool/src/process.rs (L1109-1110)
```rust
    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);
```

**File:** tx-pool/src/pool.rs (L298-304)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };
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
