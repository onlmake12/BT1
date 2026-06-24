All code references have been verified against the actual repository. The bug is confirmed:

- `remove_entry_and_descendants` at [1](#0-0)  pre-removes all links via `remove_entry_links` before calling `remove_entry` on each entry.
- `remove_entry_links` calls `self.links.remove(id)` at [2](#0-1) , erasing the key from `TxLinksMap::inner`.
- When `remove_entry` subsequently calls `update_ancestors_index_key` at [3](#0-2) , `calc_ancestors` calls `calc_relative_ids` which does `self.inner.get(short_id)` at [4](#0-3)  — returning `None` because the key was already removed — yielding an empty ancestor set.
- The `for anc_id in &ancestors` loop at [5](#0-4)  never executes; `sub_descendant_weight` is never called on surviving ancestors.
- The `EvictKey` at [6](#0-5)  uses `descendants_feerate` and `descendants_count`, both permanently inflated.
- `limit_size` at [7](#0-6)  relies on this ordering for eviction decisions.

---

Audit Report

## Title
`descendants_*` Fields and `evict_key` of Surviving Ancestors Not Updated After `remove_entry_and_descendants` — (`tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::remove_entry_and_descendants`, all parent-child links are torn down for every entry being removed before `remove_entry` is called on each. Because `update_ancestors_index_key` relies on those links to locate surviving ancestors, it finds nothing and silently skips the `sub_descendant_weight` update. Ancestor entries that remain in the pool are left with permanently inflated `descendants_count`, `descendants_fee`, `descendants_size`, `descendants_cycles`, and a stale `evict_key`, corrupting the eviction ordering used by `limit_size` every time a transaction with in-pool parents is removed.

## Finding Description
`remove_entry_and_descendants` (lines 252–265) first collects the target and all its descendants, then calls `remove_entry_links` for **all** of them in a loop, and only afterwards calls `remove_entry` for each:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← tears down ALL links first
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

`remove_entry_links` (lines 418–430) calls `self.links.remove(id)` (line 429), which removes the entry's key entirely from `TxLinksMap::inner`. When `remove_entry` subsequently calls `update_ancestors_index_key` (line 242), that function calls `self.links.calc_ancestors(&child.proposal_short_id())` (lines 433–434). `calc_ancestors` delegates to `calc_relative_ids` → `calc_relation_ids` (links.rs lines 37–72), which looks up `self.inner.get(short_id)` — returning `None` because the key was already removed. The result is an empty ancestor set; the `for anc_id in &ancestors` loop (lines 435–444) never executes. No surviving ancestor ever receives `sub_descendant_weight`, and no `evict_key` is recomputed.

The inline comment on line 256 acknowledges the intent to suppress `update_descendants_index_key` (since all descendants are being removed anyway), but the same early link-removal also silently suppresses `update_ancestors_index_key` for **surviving** ancestors — which is the defect.

`remove_entry_and_descendants` is called from every major removal path: `resolve_conflict`, `resolve_conflict_header_dep`, `limit_size`, `remove_by_detached_proposal`, `check_and_record_ancestors`, and the `remove_tx` RPC path.

## Impact Explanation
The `EvictKey` (entry.rs lines 234–247) is computed as `descendants_feerate.max(own_feerate)` with `descendants_count` as a tiebreaker. An inflated `descendants_feerate` or `descendants_count` causes an ancestor to sort later in the eviction order (i.e., it appears harder to evict). When the pool is full, `limit_size` (pool.rs lines 292–329) calls `next_evict_entry` → `iter_by_evict_key` and evicts the entry that sorts first. A stale ancestor with an inflated key is skipped, and other legitimately higher-value transactions are evicted instead. An unprivileged attacker can exploit this to keep a low-fee transaction in the pool indefinitely at low cost, and to cause legitimate high-fee transactions to be evicted in its place. The stale state accumulates with every removal cycle, progressively degrading eviction accuracy across the entire node. This constitutes a **High** impact: a vulnerability that can cause CKB network congestion with few costs.

## Likelihood Explanation
The bug fires on every call to `remove_entry_and_descendants` where the removed entry has at least one surviving in-pool ancestor. This is a routine event triggered by conflict resolution on every new transaction submission, RBF replacement, pool-full eviction, and proposal-window expiry. No special privilege is required. The attacker only needs to submit standard transactions to the mempool.

**Concrete attacker-controlled path:**
1. Submit parent tx **P** with a low fee rate.
2. Submit child tx **C** spending an output of P with a high fee rate. `record_entry_descendants` → `update_ancestors_index_key(C, Add)` inflates P's `descendants_*` fields and updates P's `evict_key`.
3. Submit a conflicting tx **C′** spending the same input as C. `resolve_conflict` calls `remove_entry_and_descendants(C)`. Links are torn down before `remove_entry(C)` runs, so `update_ancestors_index_key(C, Remove)` finds no ancestors. P's `descendants_*` fields remain at the inflated values from step 2.
4. P now has a permanently inflated `evict_key`. When the pool fills, `limit_size` skips P and evicts other transactions instead.
5. Steps 1–4 can be repeated to keep P in the pool indefinitely regardless of its actual fee rate.

## Recommendation
Update surviving ancestors' descendant accounting **before** destroying the link information. In `remove_entry_and_descendants`, add a pass that calls `update_ancestors_index_key` for each entry being removed while links are still intact, then proceed with the existing link-removal loop:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Update surviving ancestors' descendant accounting BEFORE links are removed.
    for rid in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(rid).map(|e| e.inner.clone()) {
            self.update_ancestors_index_key(&entry, EntryOp::Remove);
        }
    }

    // Now remove links (suppresses update_descendants_index_key inside remove_entry,
    // which is intentional since all descendants are being removed anyway).
    for rid in &removed_ids {
        self.remove_entry_links(rid);
    }

    removed_ids
        .iter()
        .filter_map(|rid| self.remove_entry(rid))
        .collect()
}
```

## Proof of Concept
Using the existing test infrastructure in `tx-pool/src/component/tests/pending.rs`:

1. Build parent tx **P** and child tx **C** where C spends an output of P.
2. Add both to the pool via `add_entry`. After `add_entry(C)`, assert P's `descendants_count == 2` and `descendants_fee == P.fee + C.fee` (this passes — the add path is correct).
3. Call `pool_map.remove_entry_and_descendants(&C.proposal_short_id())`.
4. Retrieve P's entry and assert `descendants_count == 1` and `descendants_fee == P.fee`. **This assertion fails** — P still reports `descendants_count == 2` and the combined fee, because `update_ancestors_index_key` was a no-op due to pre-removed links.
5. Add a third unrelated tx **Q** with a fee rate between P's own rate and P's (stale) inflated rate. Call `pool_map.next_evict_entry(Status::Pending)`. With the inflated `evict_key`, P sorts after Q and is not selected for eviction, even though P's true fee rate is lower than Q's.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L242-242)
```rust
            self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
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

**File:** tx-pool/src/component/pool_map.rs (L429-429)
```rust
        self.links.remove(id);
```

**File:** tx-pool/src/component/pool_map.rs (L435-444)
```rust
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

**File:** tx-pool/src/component/links.rs (L42-47)
```rust
        let direct = self
            .inner
            .get(short_id)
            .map(|link| link.get_direct_ids(relation))
            .cloned()
            .unwrap_or_default();
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

**File:** tx-pool/src/pool.rs (L292-329)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
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
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
        ret
    }
```
