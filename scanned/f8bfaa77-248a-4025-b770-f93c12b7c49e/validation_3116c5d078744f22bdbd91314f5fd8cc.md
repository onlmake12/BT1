Audit Report

## Title
Stale Ancestor `descendants_fee` After Subtree Link Pre-Removal Enables Eviction Bypass - (File: `tx-pool/src/component/pool_map.rs`)

## Summary
`remove_entry_and_descendants` pre-removes all link entries for the entire removed subtree before calling `remove_entry` on each node. When `remove_entry` subsequently calls `update_ancestors_index_key`, the `calc_ancestors` lookup finds no link record for the already-unlinked root and returns an empty set, so surviving ancestors never receive `sub_descendant_weight`. Their `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` remain permanently inflated. An attacker can exploit this via repeated RBF replacements to make a low-fee parent transaction appear highly valuable, preventing its eviction and enabling pool-filling attacks that cause legitimate transactions to be rejected with `Reject::Full`.

## Finding Description

`remove_entry_and_descendants` collects the root and all its descendants, then strips every link entry before calling `remove_entry`:

```rust
// tx-pool/src/component/pool_map.rs L252-265
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← strips root's link entry first
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

`remove_entry_links` (L418-430) calls `self.links.remove(id)`, which deletes the entry from `TxLinksMap::inner`. When `remove_entry` is subsequently called for the root, it invokes `update_ancestors_index_key` (L242), which calls `self.links.calc_ancestors(&child.proposal_short_id())` (L434).

`calc_ancestors` delegates to `calc_relative_ids` in `links.rs`:

```rust
// tx-pool/src/component/links.rs L37-50
fn calc_relative_ids(&self, short_id: &ProposalShortId, relation: Relation) -> HashSet<ProposalShortId> {
    let direct = self
        .inner
        .get(short_id)          // ← returns None: root was already removed
        .map(|link| link.get_direct_ids(relation))
        .cloned()
        .unwrap_or_default();   // ← empty set

    self.calc_relation_ids(direct, relation)  // ← returns empty set
}
```

Because the root's link record is gone, `direct` is empty, `calc_relation_ids` returns an empty set, and the loop in `update_ancestors_index_key` (L435-444) never executes. The surviving ancestors of the root — which are **not** in the removed set — never receive `sub_descendant_weight`. Their `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` remain permanently inflated.

The stale `descendants_fee` directly feeds `EvictKey` computation:

```rust
// tx-pool/src/component/entry.rs L234-247
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey { fee_rate: descendants_feerate.max(feerate), ... }
    }
}
```

`resolve_conflict` — the RBF path — calls `remove_entry_and_descendants` for every conflicting transaction (L309-327), making this reachable by any tx-pool submitter. The pool size-limit eviction loop in `pool.rs` (L298-328) also calls `remove_entry_and_descendants`, so the same stale accounting affects eviction under memory pressure.

The comment in `remove_entry_and_descendants` ("so that we won't update_descendants_index_key in remove_entry") reveals the intent was only to suppress redundant descendant updates, but it inadvertently also suppresses the necessary ancestor updates for the root's surviving parents.

## Impact Explanation

A surviving ancestor transaction retains an inflated `descendants_fee`. Its `EvictKey` is computed as `descendants_feerate.max(feerate)`. With an artificially high `descendants_feerate`, the entry ranks as highly valuable and is never selected by `next_evict_entry` (which iterates `iter_by_evict_key()` in ascending order). An attacker can keep a near-zero-fee parent permanently in the pool and fill the pool with such entries, causing legitimate transactions to be rejected with `Reject::Full`. This matches the **High** impact: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs." The `total_tx_size` aggregate has a `recompute_total_stat` correction path on underflow; `descendants_fee` has no such correction path.

## Likelihood Explanation

Any node's RPC endpoint accepts `send_transaction` from unprivileged callers. RBF is enabled whenever `min_rbf_rate > min_fee_rate`. The attacker pays incrementally higher fees per replacement (RBF requirement), but each replacement permanently inflates the parent's `descendants_fee` by the replaced child's fee. After N replacements the inflation is `sum(child_fees[0..N-1])`, which grows without bound. No privileged access, key material, or majority hashpower is required. The cost per inflation step is bounded only by the RBF fee increment, which can be set to the minimum allowed delta.

## Recommendation

Before calling `remove_entry_links` for the subtree root, collect the root's surviving ancestors and apply `sub_descendant_weight` to each of them. Concretely, in `remove_entry_and_descendants`, compute `calc_ancestors(id)` for the root **before** any link removal, then iterate over those ancestor IDs and call `sub_descendant_weight` with the root entry's weight. This mirrors the logic already present in `update_ancestors_index_key` but executed before links are torn down. Alternatively, restructure `remove_entry_and_descendants` so that `remove_entry` is called before `remove_entry_links` for the root, preserving the existing `update_ancestors_index_key` logic, while still suppressing descendant updates for the nodes being removed together.

## Proof of Concept

```
// Setup: P is a low-fee parent, C1 is a high-fee child spending P's output.
// After adding both:
//   P.descendants_fee = P.fee + C1.fee
//   P.evict_key reflects high descendants_feerate

// Step 1: Attacker submits C1' (higher fee, same input as C1) → RBF triggers resolve_conflict:
//   remove_entry_and_descendants(C1_id)
//     → remove_entry_links(C1_id)   // severs P→C1 link; P no longer in links.inner[C1_id]
//     → remove_entry(C1_id)
//         → update_ancestors_index_key(C1, Remove)
//             → calc_ancestors(C1_id) == {} (link already gone)
//             → P.descendants_fee NOT decremented  ← BUG
//   C1' added as new child of P → P.descendants_fee += C1'.fee

// After one replacement:
//   P.descendants_fee = P.fee + C1.fee (stale) + C1'.fee

// Step 2: Attacker submits C1'' (higher fee, same input as C1') → same path:
//   P.descendants_fee = P.fee + C1.fee + C1'.fee (stale) + C1''.fee

// After N replacements:
//   P.descendants_fee = P.fee + sum(C1..CN fees) + CN'.fee  (grows without bound)
//   P.evict_key = max(descendants_feerate, feerate) → always high → P never evicted

// Attacker repeats with many low-fee parents; pool fills; legitimate txs receive Reject::Full.
```