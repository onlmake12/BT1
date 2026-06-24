The code is confirmed. Let me verify the key details before rendering a verdict.

**`remove_entry_and_descendants` (L252-265):** Links are stripped for all entries in the subtree *before* `remove_entry` is called. [1](#0-0) 

**`remove_entry` (L235-250):** Calls `update_ancestors_index_key` with `EntryOp::Remove`, which internally calls `self.links.calc_ancestors(child_id)`. [2](#0-1) 

**`update_ancestors_index_key` (L432-445):** Resolves ancestors via `self.links.calc_ancestors`. Since links were already erased, this returns an empty set — `sub_descendant_weight` is never called on any surviving ancestor. [3](#0-2) 

**`add_entry` path (L216):** Correctly calls `record_entry_descendants` → `update_ancestors_index_key(entry, EntryOp::Add)` while links are intact. [4](#0-3) 

**`resolve_conflict` (L305-332):** Reachable by any unprivileged user via `send_transaction` RPC; calls `remove_entry_and_descendants` on conflicting pool entries. [5](#0-4) 

**`EvictKey` computation (entry.rs L234-247):** `descendants_fee` directly feeds `descendants_feerate`, which sets `EvictKey.fee_rate`. Inflated `descendants_fee` → inflated `fee_rate` → entry pushed to back of eviction queue. [6](#0-5) 

The bug is real, the exploit path is realistic, and the impact is concrete. Proceeding to output.

---

Audit Report

## Title
Asymmetric Descendant-Weight Accounting in `remove_entry_and_descendants` Allows Inflation of Ancestor's Eviction Key - (File: `tx-pool/src/component/pool_map.rs`)

## Summary

`PoolMap::remove_entry_and_descendants` erases all link records for the removed subtree before invoking `remove_entry` on each entry. Because `update_ancestors_index_key` resolves ancestors through `self.links.calc_ancestors`, and those links are already gone, `sub_descendant_weight` is never called on any surviving ancestor. The symmetric `add_descendant_weight` call in `add_entry` is never reversed, leaving `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` permanently inflated on surviving ancestors. An unprivileged attacker can exploit this via repeated conflict submissions to make a low-fee parent transaction appear to have an arbitrarily high descendant fee rate, rendering it eviction-resistant and allowing it to occupy pool space indefinitely.

## Finding Description

**Add path (correct):**

`add_entry` calls `record_entry_descendants`, which calls `update_ancestors_index_key(entry, EntryOp::Add)` while links are fully intact. `calc_ancestors` returns the correct ancestor set, and `add_descendant_weight` is called on each ancestor.

```rust
// tx-pool/src/component/pool_map.rs L216
self.record_entry_descendants(&entry);
```

**Remove path (broken):**

`remove_entry_and_descendants` first strips all link records for every entry in the subtree:

```rust
// L257-259
for id in &removed_ids {
    self.remove_entry_links(id);
}
```

The comment reads: *"update links state for remove, so that we won't update_descendants_index_key in remove_entry"*. This correctly prevents updating `ancestors_*` fields on entries that are themselves being removed — but it has the unintended side effect of also breaking `update_ancestors_index_key`.

Inside `remove_entry` (L242):

```rust
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

`update_ancestors_index_key` calls `self.links.calc_ancestors(&child.proposal_short_id())`. Because `remove_entry_links` already removed the child's link entry and unlinked it from its parents, `calc_ancestors` returns an empty set. `sub_descendant_weight` is never called on any surviving ancestor.

**Exploit cycle:**

1. Attacker submits `tx_parent` (low fee, e.g. 1 shannon) → pool accepts it; `tx_parent.descendants_fee = 1`.
2. Attacker submits `tx_child_N` spending `tx_parent`'s output O1 (high fee, e.g. 1000 shannons) → `add_entry` → `tx_parent.descendants_fee += 1000`.
3. Attacker submits `tx_conflict_N` also spending O1 → `resolve_conflict` → `remove_entry_and_descendants(tx_child_N)` → links erased → `calc_ancestors` returns ∅ → `tx_parent.descendants_fee` **not decremented** → `tx_conflict_N` added → `tx_parent.descendants_fee += tx_conflict_N.fee`.
4. Repeat from step 2 with `tx_conflict_N` as the new child to displace.

After N cycles: `tx_parent.descendants_fee ≈ 1 + N × (child_fee + conflict_fee)` while the true value should be bounded by the single live descendant's fee.

**Why existing checks do not prevent this:**

The link-erasure is intentional and there is no guard that saves the ancestor set before links are torn down. The `remove_entry_links` call inside `remove_entry` (L245) is a no-op because links were already removed, so there is no second chance to correct the accounting.

## Impact Explanation

The stale `descendants_fee` directly inflates `EvictKey.fee_rate` for `tx_parent`:

```rust
let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
EvictKey { fee_rate: descendants_feerate.max(feerate), ... }
```

Since pool eviction selects the entry with the **lowest** `EvictKey`, an inflated entry is pushed to the back of the eviction queue. A low-fee parent transaction becomes effectively eviction-resistant and occupies pool capacity indefinitely. An attacker controlling multiple such parent transactions can fill the pool with entries that appear high-fee but are not, preventing legitimate transactions from entering and causing **CKB network congestion with few costs** — matching the High impact class (10001–15000 points).

## Likelihood Explanation

The attack requires only the ability to call `send_transaction` via the public RPC — available to any unprivileged user with network access. No keys, mining power, or special roles are needed. The attacker needs one confirmed UTXO to fund `tx_parent` and can then cycle conflict transactions spending `tx_parent`'s unconfirmed output indefinitely. Because neither `tx_child_N` nor `tx_conflict_N` is ever confirmed, the attacker does not lose funds on-chain; the only cost is the computational overhead of RPC calls. The cycle can be automated in a tight loop.

## Recommendation

Before erasing link records in `remove_entry_and_descendants`, collect and decrement the surviving ancestors of the root entry while links are still intact:

1. Before the link-removal loop, identify ancestors of the root entry that are **not** in `removed_ids`.
2. Call `update_ancestors_index_key(root_entry, EntryOp::Remove)` (and for each removed entry whose ancestors are not also in the removed set) while links are intact.
3. Only then proceed to strip links and call `remove_entry`.

Alternatively, refactor `update_ancestors_index_key` to accept a pre-computed `HashSet<ProposalShortId>` so that the ancestor lookup is decoupled from the live link state, making link teardown order irrelevant.

## Proof of Concept

**Setup:** `tx_parent` in pool with `fee = 1 shannon`, `size = 100`. Initial state: `tx_parent.descendants_fee = 1`, `tx_parent.descendants_count = 1`.

**Cycle (repeat N times):**

1. Submit `tx_child_N` spending O1 (tx_parent's output), `fee = 1000 shannons`.
   - `add_entry` → `update_ancestors_index_key(tx_child_N, Add)` → `tx_parent.descendants_fee += 1000`.

2. Submit `tx_conflict_N` also spending O1, `fee = F shannons`.
   - `resolve_conflict` → `remove_entry_and_descendants(tx_child_N)` → `remove_entry_links` erases tx_child_N's link → `calc_ancestors` returns ∅ → `tx_parent.descendants_fee` **not decremented**.
   - `tx_conflict_N` added → `tx_parent.descendants_fee += F`.

**After N cycles:**

```
tx_parent.descendants_fee  ≈ 1 + N × (1000 + F)   [should be ≤ 1 + F]
tx_parent.EvictKey.fee_rate ≈ N × (1000 + F) / 100  [should be ~F/100]
```

A unit test can assert this invariant: after adding and then conflict-removing a child, `tx_parent.descendants_fee` must equal `tx_parent.fee` (i.e., reset to self-only). The test will fail on the current code, confirming the bug.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L213-220)
```rust
        evicts = self.check_and_record_ancestors(&mut entry)?;
        self.record_entry_edges(&entry)?;
        self.insert_entry(&entry, status);
        self.record_entry_descendants(&entry);
        self.track_entry_statics(None, Some(status));
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
        Ok((true, evicts))
```

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

**File:** tx-pool/src/component/pool_map.rs (L432-445)
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
