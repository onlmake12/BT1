The code trace confirms a real vulnerability. Here is the complete analysis:

**Root cause — `remove_entry_and_descendants` pre-removes links before calling `remove_entry`, silently skipping ancestor state updates.**

In `remove_entry_and_descendants`: [1](#0-0) 

All link entries are torn down first via `remove_entry_links`, then `remove_entry` is called. Inside `remove_entry`, `update_ancestors_index_key` is invoked: [2](#0-1) 

That function calls `self.links.calc_ancestors(&child.proposal_short_id())`: [3](#0-2) 

But `calc_ancestors` starts by looking up the child's own link entry: [4](#0-3) 

Because `remove_entry_links` already called `self.links.remove(id)` for C: [5](#0-4) 

`self.inner.get(C_id)` returns `None`, `direct` is empty, and `calc_ancestors` returns an empty set. The loop body in `update_ancestors_index_key` never executes — P's `sub_descendant_weight` is never called, and `e.evict_key = e.inner.as_evict_key()` is never re-run.

**The `EvictKey` is computed as `descendants_feerate.max(feerate)`:** [6](#0-5) 

So P's `evict_key.fee_rate` was elevated when C was added, and it is never corrected when C is removed via `remove_entry_and_descendants`.

---

### Title
Stale `EvictKey` on Parent After Child Removed via `remove_entry_and_descendants` Allows Low-Fee Tx to Evade Pool Eviction — (`tx-pool/src/component/pool_map.rs`)

### Summary
When a child transaction C is removed from the pool through `remove_entry_and_descendants` (the path taken by `resolve_conflict`, `limit_size`, and `remove_tx`), the parent transaction P's `descendants_fee`/`descendants_size`/`descendants_cycles` fields and its indexed `evict_key` are never updated. Because `EvictKey.fee_rate = max(descendants_feerate, feerate)`, P retains the inflated fee rate contributed by C indefinitely, making P appear more valuable than it is in the eviction ordering.

### Finding Description
`remove_entry_and_descendants` first strips all link entries for every tx in the removal set, then calls `remove_entry` on each. `remove_entry` delegates ancestor state correction to `update_ancestors_index_key`, which uses `links.calc_ancestors` to find which entries to update. Because the link entry for C was already deleted, `calc_ancestors(C)` returns an empty set, so no ancestor (including P) ever has `sub_descendant_weight` called on it, and no ancestor's `evict_key` is recalculated.

The same pre-deletion of links is intentional for the *descendant* direction (to avoid updating entries that are themselves being removed), but it is an unintended side-effect for the *ancestor* direction: ancestors of the removed subtree are not being removed, yet their descendant-accounting state is left stale.

### Impact Explanation
An unprivileged attacker can permanently inflate P's `evict_key.fee_rate` in the pool:

1. Submit P with a very low fee rate (e.g., 1 shannon/byte). P's `evict_key.fee_rate` = 1.
2. Submit C, a child of P spending P's output AND an external UTXO_C, with a very high fee rate (e.g., 1000 shannons/byte). P's `evict_key.fee_rate` is now elevated to ≈1000 via `descendants_feerate.max(feerate)`.
3. Submit C', which spends UTXO_C but not P's output, conflicting with C. `resolve_conflict` calls `remove_entry_and_descendants(C)`. Due to the bug, P's `evict_key.fee_rate` stays at ≈1000.
4. P now occupies pool space with a falsely high eviction priority. Any legitimate transaction M with a true fee rate between 1 and 1000 will be evicted before P when the pool is full.
5. The attacker can repeat steps 2–3 to keep refreshing the inflation, or simply leave P in the pool indefinitely.

The attack costs only two on-chain-valid (but unconfirmed) transactions and one conflicting submission. It requires no privileged access, no hashpower, and no social engineering.

### Likelihood Explanation
The attack is straightforward to execute by any transaction submitter. The conflicting submission path (`resolve_conflict`) is a standard, always-enabled code path. No special node configuration is required. The cost is minimal (two low-fee transactions and one conflicting transaction). The bug is deterministic and reproducible in a local test environment.

### Recommendation
In `remove_entry_and_descendants`, update ancestor state *before* removing link entries, or pass the pre-removal ancestor set explicitly to `remove_entry`. One concrete fix: collect each removed entry's ancestor set before calling `remove_entry_links`, then apply `sub_descendant_weight` and `evict_key` recalculation to those ancestors after the removal.

Alternatively, restructure `remove_entry` to accept an optional pre-computed ancestor set, bypassing the `calc_ancestors` call when the links have already been torn down.

### Proof of Concept
```rust
// In tx-pool/src/component/tests/pending.rs (illustrative)
#[test]
fn test_evict_key_stale_after_child_removed_via_conflict() {
    let mut pool = PoolMap::new(1000);

    // P: low fee (1 shannon, weight W)
    let tx_p = build_tx(vec![(&Byte32::zero(), 0), (&h256!("0x1").into(), 0)], 2);
    // C: high fee child of P, also spends external UTXO_C
    let tx_c = build_tx(vec![(&tx_p.hash(), 0), (&h256!("0x2").into(), 0)], 1);
    // C': conflicts with C on UTXO_C, does NOT spend P's output
    let tx_c_prime = build_tx(vec![(&h256!("0x2").into(), 0)], 1);
    // M: medium fee, independent
    let tx_m = build_tx(vec![(&h256!("0x3").into(), 0)], 1);

    let entry_p = TxEntry::dummy_resolve(tx_p.clone(), 2, Capacity::shannons(1), 100);
    let entry_c = TxEntry::dummy_resolve(tx_c.clone(), 2, Capacity::shannons(1000), 100);
    let entry_m = TxEntry::dummy_resolve(tx_m.clone(), 2, Capacity::shannons(50), 100);

    pool.add_entry(entry_p, Status::Pending).unwrap();
    pool.add_entry(entry_c, Status::Pending).unwrap();

    // Simulate conflict: remove C via resolve_conflict path
    pool.resolve_conflict(&tx_c_prime.transaction());

    pool.add_entry(entry_m, Status::Pending).unwrap();

    // With the bug: P's evict_key.fee_rate is still ~1000, so M is evicted first
    let first_evict = pool.next_evict_entry(Status::Pending).unwrap();
    // Expected (correct): first_evict == tx_p (fee_rate 1 < 50)
    // Actual (buggy):     first_evict == tx_m (because P's stale evict_key shows ~1000)
    assert_eq!(first_evict, tx_p.proposal_short_id(),
        "P should be evicted first (lowest true fee rate), not M");
}
```

### Citations

**File:** tx-pool/src/component/pool_map.rs (L242-243)
```rust
            self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
            self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
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
