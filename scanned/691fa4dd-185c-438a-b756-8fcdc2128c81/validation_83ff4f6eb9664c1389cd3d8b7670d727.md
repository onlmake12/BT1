### Title
Asymmetric Descendant-Weight Accounting in `remove_entry_and_descendants` Allows Attacker to Inflate Pool Entry's Effective Fee Rate - (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::remove_entry_and_descendants` pre-removes all link records before calling `remove_entry`, which causes `update_ancestors_index_key` to find zero ancestors and skip decrementing their `descendants_fee / descendants_size / descendants_cycles / descendants_count`. Because `add_entry` correctly increments those fields, the accounting is permanently asymmetric. An unprivileged tx-pool submitter can exploit this to inflate a low-fee parent transaction's apparent descendant fee rate, making it eviction-resistant and allowing it to occupy pool space indefinitely.

---

### Finding Description

**Add path (correct):**

When `add_entry` is called, `record_entry_descendants` is invoked at the end:

```rust
// update ancestor's index key for adding new entry
self.update_ancestors_index_key(entry, EntryOp::Add);
```

`update_ancestors_index_key` walks `self.links.calc_ancestors(child_id)` and calls `add_descendant_weight(child)` on every ancestor still in the pool. [1](#0-0) 

**Remove path (broken):**

`remove_entry_and_descendants` first strips **all** link records for every entry in the subtree, then calls `remove_entry` for each:

```rust
// update links state for remove, so that we won't update_descendants_index_key in remove_entry
for id in &removed_ids {
    self.remove_entry_links(id);
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
``` [2](#0-1) 

Inside `remove_entry`, `update_ancestors_index_key` is called with `EntryOp::Remove`:

```rust
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
``` [3](#0-2) 

But `update_ancestors_index_key` resolves ancestors via `self.links.calc_ancestors(child_id)`. Because the links were already erased, `calc_ancestors` returns an **empty set**, so `sub_descendant_weight` is never called on any surviving ancestor. [4](#0-3) 

The `add_descendant_weight` / `sub_descendant_weight` pair that should be symmetric: [5](#0-4) 

**Where `remove_entry_and_descendants` is triggered by an attacker:**

`resolve_conflict` is called every time a submitted transaction spends an input already claimed by a pool transaction. It calls `remove_entry_and_descendants` on the conflicting pool entry, leaving its ancestors with stale (inflated) descendant weights. [6](#0-5) 

---

### Impact Explanation

The stale `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` fields directly feed the `EvictKey` computation:

```rust
let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
let feerate = FeeRate::calculate(entry.fee, weight);
EvictKey {
    fee_rate: descendants_feerate.max(feerate),
    ...
}
``` [7](#0-6) 

An inflated `descendants_fee` raises `descendants_feerate`, which raises `EvictKey.fee_rate`. Since eviction picks the entry with the **lowest** `EvictKey`, an inflated entry is pushed to the back of the eviction queue and effectively becomes eviction-resistant. A low-fee parent transaction can be kept alive in the pool indefinitely, consuming pool capacity and distorting fee estimation.

---

### Likelihood Explanation

The attack requires only the ability to submit transactions via the `send_transaction` RPC — available to any unprivileged user. No special role, key, or hashpower is needed. The attacker needs only two UTXOs: one to fund `tx_parent` and one to fund the repeated conflict transactions. The cycle (submit child → submit conflicting tx → repeat) can be executed in a tight loop with minimal cost because the conflict transactions themselves are never confirmed.

---

### Recommendation

Before erasing link records in `remove_entry_and_descendants`, collect the surviving ancestors of the root entry and decrement their descendant weights. Concretely:

1. Before the link-removal loop, call `update_ancestors_index_key(root_entry, EntryOp::Remove)` for the root (and each removed entry whose ancestors are not also in the removed set) while links are still intact.
2. Only then proceed to strip links and call `remove_entry`.

Alternatively, restructure `remove_entry` to accept a pre-computed ancestor set so that the link teardown order does not matter.

---

### Proof of Concept

Assume `tx_parent` is in the pool with `fee = 1 shannon`, `size = 100`.

**Initial state:** `tx_parent.descendants_fee = 1`, `tx_parent.descendants_count = 1`.

**Cycle (repeat N times):**

1. Submit `tx_child_N` spending `tx_parent`'s output with `fee = 1000 shannons`.
   - `add_entry` → `update_ancestors_index_key(tx_child_N, Add)` → `tx_parent.descendants_fee += 1000`, `tx_parent.descendants_count += 1`.

2. Submit `tx_conflict_N` spending the **same input** as `tx_child_N` (a different UTXO the attacker controls, or a double-spend of `tx_child_N`'s input).
   - `resolve_conflict` → `remove_entry_and_descendants(tx_child_N)` → links erased first → `update_ancestors_index_key(tx_child_N, Remove)` finds **no ancestors** → `tx_parent.descendants_fee` is **not decremented**.
   - `tx_conflict_N` is added; `tx_parent.descendants_fee += tx_conflict_N.fee`.

After N cycles:

```
tx_parent.descendants_fee  ≈ 1 + N × 1000  (should be ≤ 1001)
tx_parent.EvictKey.fee_rate ≈ N × 1000 / 100  (should be ~10)
```

`tx_parent` is now effectively immune to eviction despite having a real fee rate of `1/100 = 0.01 shannon/byte`. [2](#0-1)

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

**File:** tx-pool/src/component/entry.rs (L120-142)
```rust
    /// Update ancestor state for add an entry
    pub fn add_descendant_weight(&mut self, entry: &TxEntry) {
        self.descendants_count = self.descendants_count.saturating_add(1);
        self.descendants_size = self.descendants_size.saturating_add(entry.size);
        self.descendants_cycles = self.descendants_cycles.saturating_add(entry.cycles);
        self.descendants_fee = Capacity::shannons(
            self.descendants_fee
                .as_u64()
                .saturating_add(entry.fee.as_u64()),
        );
    }

    /// Update ancestor state for remove an entry
    pub fn sub_descendant_weight(&mut self, entry: &TxEntry) {
        self.descendants_count = self.descendants_count.saturating_sub(1);
        self.descendants_size = self.descendants_size.saturating_sub(entry.size);
        self.descendants_cycles = self.descendants_cycles.saturating_sub(entry.cycles);
        self.descendants_fee = Capacity::shannons(
            self.descendants_fee
                .as_u64()
                .saturating_sub(entry.fee.as_u64()),
        );
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
