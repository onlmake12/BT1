Audit Report

## Title
Ancestors' Descendant-Weight State Not Updated in `remove_entry_and_descendants()` Leaves Pool Entries with Stale Eviction Keys — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::remove_entry_and_descendants()` strips all link records for every entry in the removal set before delegating to `remove_entry()`. Because `update_ancestors_index_key()` resolves ancestors through those same link records, any ancestor that remains in the pool never receives the `sub_descendant_weight` call. Its `descendants_count`, `descendants_fee`, `descendants_size`, and `descendants_cycles` fields are permanently inflated, producing a stale `EvictKey` that causes `limit_size` to skip the entry during eviction, enabling pool-congestion DoS by any unprivileged transaction submitter.

## Finding Description

`remove_entry_and_descendants` collects the root and all its descendants, strips every link record up front, then delegates to `remove_entry` for each id: [1](#0-0) 

The comment on line 256 states the intent — pre-removing links prevents `update_descendants_index_key` from trying to update entries that are themselves being removed. However, it also silently breaks `update_ancestors_index_key`, which is called inside `remove_entry` immediately after the entry is removed from `self.entries`: [2](#0-1) 

`update_ancestors_index_key` resolves ancestors exclusively through `self.links.calc_ancestors()`: [3](#0-2) 

`calc_ancestors` performs a BFS/DFS over `TxLinksMap::inner`: [4](#0-3) 

Because `remove_entry_links` already called `self.links.remove(id)` for every entry in the removal set — including removing those ids from their parents' children sets — the traversal finds nothing. The `sub_descendant_weight` call is never made for any ancestor that remains in the pool. [5](#0-4) 

By contrast, the single-entry `remove_entry` path calls `update_ancestors_index_key` (line 242) **before** `remove_entry_links` (line 245), so ancestors are correctly decremented in that path.

The stale fields feed directly into `EvictKey`: [6](#0-5) 

`EvictKey.Ord` compares `fee_rate` first; lower fee_rate = lower key = evicted first: [7](#0-6) 

`next_evict_entry` iterates entries in ascending `evict_key` order: [8](#0-7) 

An ancestor with an inflated `descendants_feerate` sorts higher (appears more valuable) and is skipped. `limit_size` therefore cannot evict it: [9](#0-8) 

The trigger path is `resolve_conflict`, called on every transaction submission that spends an already-spent input: [10](#0-9) 

## Impact Explanation

A low-fee ancestor transaction that survives `remove_entry_and_descendants` carries permanently inflated `descendants_*` fields. When the pool reaches `max_tx_pool_size`, `limit_size` iterates by ascending `EvictKey` and skips the stale ancestor because its inflated `descendants_feerate` makes it appear more valuable than it is. Legitimate incoming transactions are rejected with `Reject::Full` while the low-fee stale entry occupies pool space indefinitely. An attacker can repeat this pattern to keep arbitrarily many low-fee transactions pinned in the pool, causing sustained pool congestion.

This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

The trigger is `resolve_conflict`, called on every transaction submission that spends an already-spent input. Any unprivileged RPC caller can invoke `send_transaction` with a double-spend to reach this path. No special privilege, key material, or majority hash power is required. The attack is cheap (only transaction fees for the initial chain and the conflicting transaction), repeatable, and leaves a permanent residue per invocation.

## Recommendation

Move the ancestor-weight update **before** link removal in `remove_entry_and_descendants`. Before the loop that calls `remove_entry_links`, walk the ancestors of the root entry (while links are still intact) and call `sub_descendant_weight` on each remaining ancestor for every entry being removed. The single-entry `remove_entry` path already implements this correctly and can serve as the reference. Alternatively, refactor `remove_entry_and_descendants` to call `update_ancestors_index_key` explicitly for each removed entry before any links are torn down.

## Proof of Concept

1. Submit `tx0` (low fee, spends a confirmed cell output).
2. Submit `tx1` (child of `tx0`, very high fee). At this point `tx0.descendants_count = 2`, `tx0.descendants_fee` is large.
3. Submit `tx2`, `tx3` as further descendants of `tx1`. Now `tx0.descendants_count = 4`.
4. Submit `tx1'` spending the same input as `tx1` (double-spend). `resolve_conflict` fires and calls `remove_entry_and_descendants(tx1_id)`.
5. `tx1`, `tx2`, `tx3` are removed. `tx0` remains with `descendants_count = 4` (correct value: `1`) and inflated `descendants_fee`/`descendants_size`/`descendants_cycles`.
6. Verify: inspect `tx0`'s `EvictKey`; its `fee_rate` field reflects the inflated `descendants_feerate` rather than `tx0`'s own low fee rate.
7. Fill the pool to `max_tx_pool_size` with medium-fee transactions. Observe that `limit_size` iterates by `evict_key` and skips `tx0` because its inflated key ranks it above the medium-fee entries.
8. Submit a new legitimate transaction; it is rejected with `Reject::Full` while `tx0` (a low-fee transaction) persists in the pool.
9. Repeat steps 1–8 to accumulate multiple stale low-fee entries, progressively degrading pool availability.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L235-250)
```rust
    pub(crate) fn remove_entry(&mut self, id: &ProposalShortId) -> Option<TxEntry> {
        self.entries.remove_by_id(id).map(|entry| {
            debug!(
                "remove entry {} from status: {:?}",
                entry.inner.transaction().hash(),
                entry.status
            );
            self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
            self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
            self.remove_entry_edges(&entry.inner);
            self.remove_entry_links(id);
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
            entry.inner
        })
    }
```

**File:** tx-pool/src/component/pool_map.rs (L252-265)
```rust
    pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
        let mut removed_ids = vec![id.to_owned()];
        removed_ids.extend(self.calc_descendants(id));

        // update links state for remove, so that we won't update_descendants_index_key in remove_entry
        for id in &removed_ids {
            self.remove_entry_links(id);
        }

        removed_ids
            .iter()
            .filter_map(|id| self.remove_entry(id))
            .collect()
    }
```

**File:** tx-pool/src/component/pool_map.rs (L305-332)
```rust
    pub(crate) fn resolve_conflict(&mut self, tx: &TransactionView) -> Vec<ConflictEntry> {
        let mut conflicts = Vec::new();

        for i in tx.input_pts_iter() {
            if let Some(id) = self.edges.remove_input(&i) {
                let entries = self.remove_entry_and_descendants(&id);
                if !entries.is_empty() {
                    let reject = Reject::Resolve(OutPointError::Dead(i.clone()));
                    let rejects = std::iter::repeat_n(reject, entries.len());
                    conflicts.extend(entries.into_iter().zip(rejects));
                }
            }

            // deps consumed
            if let Some(x) = self.edges.remove_deps(&i) {
                for id in x {
                    let entries = self.remove_entry_and_descendants(&id);
                    if !entries.is_empty() {
                        let reject = Reject::Resolve(OutPointError::Dead(i.clone()));
                        let rejects = std::iter::repeat_n(reject, entries.len());
                        conflicts.extend(entries.into_iter().zip(rejects));
                    }
                }
            }
        }

        conflicts
    }
```

**File:** tx-pool/src/component/pool_map.rs (L380-385)
```rust
    pub(crate) fn next_evict_entry(&self, status: Status) -> Option<ProposalShortId> {
        self.entries
            .iter_by_evict_key()
            .find(move |entry| entry.status == status)
            .map(|entry| entry.id.clone())
    }
```

**File:** tx-pool/src/component/pool_map.rs (L418-430)
```rust
    fn remove_entry_links(&mut self, id: &ProposalShortId) {
        if let Some(parents) = self.links.get_parents(id).cloned() {
            for parent in parents {
                self.links.remove_child(&parent, id);
            }
        }
        if let Some(children) = self.links.get_children(id).cloned() {
            for child in children {
                self.links.remove_parent(&child, id);
            }
        }
        self.links.remove(id);
    }
```

**File:** tx-pool/src/component/pool_map.rs (L432-444)
```rust
    fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
        let ancestors: HashSet<ProposalShortId> =
            self.links.calc_ancestors(&child.proposal_short_id());
        for anc_id in &ancestors {
            // update parent score
            self.entries.modify_by_id(anc_id, |e| {
                match op {
                    EntryOp::Remove => e.inner.sub_descendant_weight(child),
                    EntryOp::Add => e.inner.add_descendant_weight(child),
                };
                e.evict_key = e.inner.as_evict_key();
            });
        }
```

**File:** tx-pool/src/component/links.rs (L37-50)
```rust
    fn calc_relative_ids(
        &self,
        short_id: &ProposalShortId,
        relation: Relation,
    ) -> HashSet<ProposalShortId> {
        let direct = self
            .inner
            .get(short_id)
            .map(|link| link.get_direct_ids(relation))
            .cloned()
            .unwrap_or_default();

        self.calc_relation_ids(direct, relation)
    }
```

**File:** tx-pool/src/component/entry.rs (L234-247)
```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);

        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            timestamp: entry.timestamp,
            descendants_count: entry.descendants_count,
        }
    }
```

**File:** tx-pool/src/component/sort_key.rs (L92-104)
```rust
impl Ord for EvictKey {
    fn cmp(&self, other: &Self) -> Ordering {
        if self.fee_rate == other.fee_rate {
            if self.descendants_count == other.descendants_count {
                self.timestamp.cmp(&other.timestamp)
            } else {
                self.descendants_count.cmp(&other.descendants_count)
            }
        } else {
            self.fee_rate.cmp(&other.fee_rate)
        }
    }
}
```

**File:** tx-pool/src/pool.rs (L298-308)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
```
