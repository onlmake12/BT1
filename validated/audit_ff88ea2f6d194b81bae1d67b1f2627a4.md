### Title
Stale Descendant Accounting in `remove_entry_and_descendants` Permanently Inflates Ancestor `evict_key`, Enabling Pool-Eviction Bypass — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

`remove_entry_and_descendants` strips all link records for every entry being removed **before** calling `remove_entry`. Because `remove_entry` relies on the live link graph to locate ancestors and call `sub_descendant_weight` on them, those ancestors are silently skipped. Every ancestor that remains in the pool retains permanently inflated `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count`, and its `evict_key` is never recomputed. An unprivileged transaction sender can exploit this via repeated RBF to make a low-fee transaction appear to have an arbitrarily high descendant fee-rate, preventing it from ever being evicted.

---

### Finding Description

`remove_entry_and_descendants` collects the target and all its descendants, strips their link records in a first pass, then calls `remove_entry` for each: [1](#0-0) 

The comment on line 256 acknowledges the intent: pre-removing links prevents `update_descendants_index_key` from touching entries that are themselves being removed. However, the same pre-removal also silences `update_ancestors_index_key` for every entry in the batch, because that function resolves ancestors through the same link graph: [2](#0-1) 

After `remove_entry_links` has run, `self.links.calc_ancestors(...)` returns an empty set for every removed entry. The `modify_by_id` loop body — which calls `sub_descendant_weight` and recomputes `evict_key` — never executes for any ancestor that is **not** in the removed set.

The per-entry subtraction that should have fired is: [3](#0-2) 

Because it is never called, the surviving ancestor's `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` all remain at their pre-removal values. The `evict_key` stored in the multi-index map is also never refreshed: [4](#0-3) 

When the replacement transaction (TxC) is subsequently added, `record_entry_descendants` calls `update_ancestors_index_key(TxC, Add)`, which **adds** TxC's weight to the already-inflated ancestor entry. The ancestor's `descendants_fee` now equals `fee_B_removed + fee_C_new` instead of just `fee_C_new`. [5](#0-4) 

---

### Impact Explanation

The `evict_key` drives the pool's eviction ordering. `next_evict_entry` iterates `iter_by_evict_key()` to find the lowest-priority transaction to drop when the pool is full: [6](#0-5) 

An inflated `descendants_feerate` raises the `fee_rate` field of `EvictKey`, pushing the ancestor toward the "do not evict" end of the ordering. A low-fee transaction with an artificially high `evict_key` will survive pool-full eviction rounds that should have removed it, causing legitimate higher-fee transactions to be rejected with `Reject::Full`. The pool's total size and cycle accounting (`total_tx_size`, `total_tx_cycles`) is correctly maintained via `update_stat_for_remove_tx`, so the pool does not grow unboundedly — but the wrong transactions occupy the fixed capacity.

---

### Likelihood Explanation

`remove_entry_and_descendants` is called on every conflict resolution path reachable by any unprivileged RPC or P2P transaction sender: [7](#0-6) 

**Concrete attack sequence (no special privilege required):**

1. Submit **TxA** with fee just above `min_fee_rate`. TxA is the target to protect.
2. Submit **TxB** (child of TxA) with a large fee `F_B`. TxA's `descendants_fee` becomes `fee_A + F_B`; its `evict_key` rises.
3. Submit **TxC** that double-spends one of TxB's inputs with fee `F_B + ε` (valid RBF). `resolve_conflict` calls `remove_entry_and_descendants(TxB)`. TxA's `descendants_fee` is **not** decremented.
4. TxC is added; `update_ancestors_index_key(TxC, Add)` fires, adding `F_C` to TxA's already-inflated `descendants_fee`. TxA now carries `fee_A + F_B + F_C` instead of `fee_A + F_C`.
5. Repeat steps 2–4 with TxD, TxE, … Each iteration adds another stale `F_i` to TxA's `descendants_fee`. After N rounds, TxA's `evict_key` reflects `fee_A + Σ F_i`, making it effectively impossible to evict.

The attacker pays incremental RBF fees per round but retains TxA in the pool indefinitely at a fraction of the cost of the inflated fee-rate it appears to have.

---

### Recommendation

Update surviving ancestors **before** stripping link records. One correct approach:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // 1. Update ancestors of every removed entry WHILE links are still intact.
    for rid in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(rid).map(|e| e.inner.clone()) {
            self.update_ancestors_index_key(&entry, EntryOp::Remove);
        }
    }

    // 2. Now strip links (prevents double-update inside remove_entry).
    for rid in &removed_ids {
        self.remove_entry_links(rid);
    }

    removed_ids
        .iter()
        .filter_map(|rid| self.remove_entry(rid))
        .collect()
}
```

This ensures every ancestor that remains in the pool has its `descendants_fee/size/cycles/count` and `evict_key` correctly decremented before the link graph is torn down.

---

### Proof of Concept

```
Initial pool:
  TxA  fee=1, size=100   (no parents)
  TxB  fee=1000, size=100 (parent: TxA)

TxA.descendants_fee   = 1001
TxA.descendants_size  = 200
TxA.evict_key.fee_rate = high  (driven by TxB)

--- Attacker submits TxC (RBF of TxB, fee=1001) ---

resolve_conflict(TxB):
  remove_entry_links(TxB)   ← TxA no longer knows TxB is its child
  remove_entry(TxB):
    update_ancestors_index_key(TxB, Remove)
      calc_ancestors(TxB) = {}   ← links gone, returns empty
      → TxA.descendants_fee NOT decremented   ← BUG

add_entry(TxC):
  update_ancestors_index_key(TxC, Add)
    calc_ancestors(TxC) = {TxA}
    TxA.descendants_fee += fee_C = 1001
    TxA.evict_key recomputed

Final state:
  TxA.descendants_fee = 1001 (stale TxB) + 1001 (TxC) = 2002
  Correct value should be: 1 (TxA) + 1001 (TxC) = 1002

Repeat N times → TxA.descendants_fee grows without bound.
TxA is never evicted regardless of pool pressure.
```

### Citations

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

**File:** tx-pool/src/component/entry.rs (L133-142)
```rust
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
