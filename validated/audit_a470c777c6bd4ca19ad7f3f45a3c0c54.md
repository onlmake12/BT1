All code references in the submitted report have been verified against the actual source. Here is the validation result:

---

Audit Report

## Title
Asymmetric Descendant-Weight Accounting in `remove_entry_and_descendants` Allows Inflation of Ancestor's Eviction Key - (File: `tx-pool/src/component/pool_map.rs`)

## Summary

`PoolMap::remove_entry_and_descendants` erases all link records for the removed subtree before invoking `remove_entry` on each entry. Because `update_ancestors_index_key` resolves ancestors through `self.links.calc_ancestors`, and those links are already gone, `sub_descendant_weight` is never called on any surviving ancestor. The symmetric `add_descendant_weight` call made during `add_entry` is never reversed, leaving `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` permanently inflated on surviving ancestors. An attacker can exploit this via repeated RBF submissions to make a low-fee parent transaction appear to have an arbitrarily high descendant fee rate, rendering it eviction-resistant and allowing it to occupy pool space indefinitely.

## Finding Description

**Add path (correct):**

`add_entry` calls `record_entry_descendants` at L216, which at L512 calls `update_ancestors_index_key(entry, EntryOp::Add)` while links are fully intact. `calc_ancestors` returns the correct ancestor set, and `add_descendant_weight` is called on each ancestor. [1](#0-0) 

**Remove path (broken):**

`remove_entry_and_descendants` first strips all link records for every entry in the subtree (L257–259), then calls `remove_entry` for each: [2](#0-1) 

Inside `remove_entry` (L242), `update_ancestors_index_key` is called with `EntryOp::Remove`: [3](#0-2) 

`update_ancestors_index_key` calls `self.links.calc_ancestors(&child.proposal_short_id())`. Because `remove_entry_links` already removed the child's link entry and unlinked it from its parents, `calc_ancestors` returns an empty set. `sub_descendant_weight` is never called on any surviving ancestor: [4](#0-3) 

`calc_ancestors` traverses `self.links.inner` starting from the child's direct parents. After `remove_entry_links`, the child's entry is gone from `inner`, so the traversal immediately returns an empty set: [5](#0-4) 

**Exploit cycle (RBF-enabled path):**

The RBF submission path in `process_rbf` calls `remove_entry_and_descendants` directly for each conflict: [6](#0-5) 

1. Attacker submits `tx_parent` (low fee, e.g. 1 shannon) → pool accepts it; `tx_parent.descendants_fee = 1`.
2. Attacker submits `tx_child` spending `tx_parent`'s output O1 (fee = F1) → `add_entry` → `tx_parent.descendants_fee += F1`.
3. Attacker submits `tx_conflict` also spending O1 (fee = F1 + extra_rbf_fee, satisfying RBF Rule #3/#4) → `check_rbf` passes → `process_rbf` → `remove_entry_and_descendants(tx_child)` → links erased → `calc_ancestors` returns ∅ → `tx_parent.descendants_fee` **not decremented** → `tx_conflict` added → `tx_parent.descendants_fee += F2`.
4. Repeat from step 2 with a new child spending O1 (fee = F2 + extra_rbf_fee).

After N cycles: `tx_parent.descendants_fee ≈ 1 + Σ(F_i)` while the true value should be bounded by the single live descendant's fee. Fees grow linearly (each cycle adds only `extra_rbf_fee` more than the previous), so many cycles are feasible before hitting any supply limit.

**Why RBF fee rules do not prevent this:**

`check_rbf` enforces that the new transaction's fee exceeds the sum of replaced transactions' fees plus `extra_rbf_fee`: [7](#0-6) 

This prevents free replacement but does not prevent the accounting corruption. Since none of the conflicting transactions are ever confirmed (they keep being replaced), the attacker pays zero on-chain fees. The only cost is RPC call overhead.

## Impact Explanation

The stale `descendants_fee` directly inflates `EvictKey.fee_rate` for `tx_parent`: [8](#0-7) 

Pool eviction selects the entry with the **lowest** `EvictKey`. An inflated entry is pushed to the back of the eviction queue. A low-fee parent transaction becomes effectively eviction-resistant and occupies pool capacity indefinitely. An attacker controlling multiple such parent transactions can fill the pool with entries that appear high-fee but are not, preventing legitimate transactions from entering.

This matches the **High (10001–15000 points)** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs.*

## Likelihood Explanation

The attack requires only the ability to call `send_transaction` via the public RPC with RBF enabled (`min_rbf_rate > min_fee_rate`). No keys, mining power, or special roles are needed beyond one confirmed UTXO to fund `tx_parent`. The attacker cycles conflicting transactions spending `tx_parent`'s unconfirmed output indefinitely. Because neither `tx_child` nor `tx_conflict` is ever confirmed, the attacker pays no on-chain fees. The cycle can be automated in a tight loop.

## Recommendation

Before erasing link records in `remove_entry_and_descendants`, collect and decrement the surviving ancestors of the root entry while links are still intact:

1. Before the link-removal loop, identify ancestors of the root entry that are **not** in `removed_ids`.
2. Call `update_ancestors_index_key(root_entry, EntryOp::Remove)` for each removed entry whose ancestors are not also in the removed set, while links are intact.
3. Only then proceed to strip links and call `remove_entry`.

Alternatively, refactor `update_ancestors_index_key` to accept a pre-computed `HashSet<ProposalShortId>` so that the ancestor lookup is decoupled from the live link state, making link teardown order irrelevant.

## Proof of Concept

**Setup:** `tx_parent` in pool with `fee = 1 shannon`, `size = 100`. Initial state: `tx_parent.descendants_fee = 1`, `tx_parent.descendants_count = 1`.

**Invariant test (will fail on current code):**

```rust
// Add tx_parent
pool.add_proposed(tx_parent_entry.clone()).unwrap();
let parent_id = tx_parent_entry.proposal_short_id();

// Add tx_child spending tx_parent's output
pool.add_proposed(tx_child_entry.clone()).unwrap();
assert!(pool.get(&parent_id).unwrap().descendants_fee > tx_parent_entry.fee);

// Remove tx_child via remove_entry_and_descendants (simulating RBF conflict removal)
pool.pool_map.remove_entry_and_descendants(&tx_child_entry.proposal_short_id());

// INVARIANT: after removing the only descendant, descendants_fee must equal self fee
let parent = pool.get(&parent_id).unwrap();
assert_eq!(parent.descendants_fee, tx_parent_entry.fee,
    "descendants_fee must reset to self-only after descendant removal");
// This assertion FAILS on current code: descendants_fee remains inflated
```

**After N cycles:**

```
tx_parent.descendants_fee  ≈ 1 + Σ(F_i)   [should be ≤ 1 + F_last]
tx_parent.EvictKey.fee_rate ≈ Σ(F_i) / 100  [should be ~F_last/100]
```

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

**File:** tx-pool/src/component/pool_map.rs (L487-513)
```rust
    fn record_entry_descendants(&mut self, entry: &TxEntry) {
        let tx_short_id: ProposalShortId = entry.proposal_short_id();
        let outputs = entry.transaction().output_pts();
        let mut children = HashSet::new();

        // collect children
        for o in outputs {
            if let Some(ids) = self.edges.get_deps_ref(&o).cloned() {
                children.extend(ids);
            }
            if let Some(id) = self.edges.get_input_ref(&o).cloned() {
                children.insert(id);
            }
        }
        // update children
        if !children.is_empty() {
            for child in &children {
                self.links.add_parent(child, tx_short_id.clone());
            }
            if let Some(links) = self.links.inner.get_mut(&tx_short_id) {
                links.children.extend(children);
            }
            self.update_descendants_index_key(entry, EntryOp::Add);
        }
        // update ancestor's index key for adding new entry
        self.update_ancestors_index_key(entry, EntryOp::Add);
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

**File:** tx-pool/src/process.rs (L203-206)
```rust
        let all_removed: Vec<_> = conflicts
            .iter()
            .flat_map(|id| tx_pool.pool_map.remove_entry_and_descendants(id))
            .collect();
```

**File:** tx-pool/src/pool.rs (L662-676)
```rust
        // Rule #4, new tx's fee need to higher than min_rbf_fee computed from the tx_pool configuration
        // Rule #3, new tx's fee need to higher than conflicts, here we only check the all conflicted txs fee
        let fee = entry.fee;
        if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
            if fee < min_replace_fee {
                return Err(Reject::RBFRejected(format!(
                    "Tx's current fee is {}, expect it to >= {} to replace old txs",
                    fee, min_replace_fee,
                )));
            }
        } else {
            return Err(Reject::RBFRejected(
                "calculate_min_replace_fee failed".to_string(),
            ));
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
