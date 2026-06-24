The code confirms the claim. Let me verify the key interaction between `remove_entry_and_descendants` and `remove_entry`:

- `remove_entry_and_descendants` (L252-265): calls `remove_entry_links(id)` for **all** removed IDs first, then calls `remove_entry(id)` for each.
- `remove_entry` (L235-250): calls `update_ancestors_index_key` at L242, which calls `calc_ancestors` — but the link for the entry was already erased, so it returns an empty set and the ancestor update loop never runs.
- `EvictKey` (entry.rs L234-247): is computed directly from `descendants_fee`, `descendants_size`, `descendants_cycles` — the exact fields that are never decremented.
- `limit_size` (pool.rs L292-329): loops on `next_evict_entry` which selects by `EvictKey`; entries with stale inflated keys are skipped, so the pool never shrinks.

The bug is real, the exploit path is reachable by any unprivileged transaction sender, and the impact matches an allowed bounty class.

---

Audit Report

## Title
Stale Descendant-Weight Statistics After Batch Removal Causes Incorrect Eviction Scoring and Tx-Pool DoS - (File: `tx-pool/src/component/pool_map.rs`)

## Summary
`remove_entry_and_descendants` erases all parent-child links for every removed entry before calling `remove_entry` on any of them. Because `remove_entry` relies on those links (via `update_ancestors_index_key` → `calc_ancestors`) to decrement surviving ancestors' `descendants_fee/size/cycles/count`, the decrement never happens. The surviving ancestors permanently carry inflated descendant-weight statistics, which inflate their `EvictKey`, suppressing their eviction and allowing an attacker to fill the pool with low-fee transactions that carry phantom descendant fees.

## Finding Description
`remove_entry_and_descendants` (pool_map.rs L252-265) first iterates all removed IDs and calls `remove_entry_links(id)` for each, which removes the entry from every parent's children set and deletes the entry's link record entirely (L418-430). Only after all links are gone does it call `remove_entry(id)` for each ID.

`remove_entry` (L235-250) calls `update_ancestors_index_key(&entry.inner, EntryOp::Remove)` at L242. That function (L432-445) calls `self.links.calc_ancestors(&child.proposal_short_id())`. Because the link for `id` was already deleted in the first loop, `calc_ancestors` returns an empty `HashSet`. The `for anc_id in &ancestors` loop body — which calls `e.inner.sub_descendant_weight(child)` and recomputes `e.evict_key` — never executes.

The result is that every ancestor of the removed subtree that was **not** itself removed retains stale (inflated) values for `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count`. `EvictKey` (entry.rs L234-247) is computed as `max(descendants_feerate, own_feerate)`, so inflated descendant fields produce an artificially high evict key. `next_evict_entry` selects the entry with the **lowest** evict key, so these ancestors are never selected for eviction.

The existing `remove_entry_links` call inside `remove_entry` itself (L245) is a no-op for the already-cleared entries and provides no correction.

## Impact Explanation
An attacker who submits a low-fee parent A and a high-fee child B, then displaces B via a cheap RBF-conflicting transaction C, leaves A in the pool with B's fee contribution permanently embedded in A's `descendants_fee`. A's `EvictKey` remains artificially high. When the pool reaches `max_tx_pool_size`, `limit_size` (pool.rs L292-329) loops calling `next_evict_entry` and `remove_entry_and_descendants`, but A is never selected. New legitimate high-fee transactions are rejected with `Reject::Full`. By repeating the pattern with many parent transactions, the attacker can fill the pool with phantom-fee entries at low cost, causing sustained CKB network congestion. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation
The attack requires no privileged access. Any P2P peer or RPC caller can submit transactions. `remove_entry_and_descendants` is triggered by `resolve_conflict` (RBF), `resolve_conflict_header_dep` (header-dep invalidation), and `limit_size` (size eviction) — all reachable without authentication. The attacker's cost per phantom-fee slot is the fee for parent A plus the minimum RBF fee bump for the conflicting transaction C. The stale state persists until A is committed on-chain or the node restarts, giving the attacker a long window per slot. The attack is repeatable and scalable.

## Recommendation
Compute the set of surviving ancestors of the root entry **before** clearing any links, then explicitly call `sub_descendant_weight` and recompute `evict_key` for each of them. Concretely:

1. Before the `remove_entry_links` loop in `remove_entry_and_descendants`, call `self.links.calc_ancestors(id)` to obtain the set of ancestors that will survive the removal.
2. After all entries are removed, iterate that ancestor set and apply `sub_descendant_weight` for each removed entry that was a descendant of each ancestor, then recompute `evict_key`.

Alternatively, restructure the function so that `remove_entry_links` is called only for the strict descendants (not the root), allowing `remove_entry`'s existing `update_ancestors_index_key` call to correctly find and update the root's surviving ancestors before the root's own links are cleared.

## Proof of Concept
```
1. Submit tx A: low fee (100 shannons), size 200 bytes. Pool state: A.descendants_fee=100.
2. Submit tx B: child of A, high fee (10_000 shannons), size 200 bytes.
   Pool state: A.descendants_fee=10_100, A.descendants_size=400.
   A.evict_key.fee_rate ≈ 25_250 shannons/kB (high → not evicted).
3. Submit tx C: spends same input as B (RBF conflict, minimum fee bump).
   resolve_conflict → remove_entry_and_descendants(B):
     remove_entry_links(B): removes B from A's children, deletes B's link record.
     remove_entry(B): update_ancestors_index_key(B, Remove):
       calc_ancestors(B) → {} (link already gone)
       → A.sub_descendant_weight(B) is NEVER called.
4. After removal:
   A.descendants_fee   = 10_100  (should be 100)
   A.descendants_size  = 400     (should be 200)
   A.evict_key.fee_rate ≈ 25_250 (should be ~500 shannons/kB)
5. Fill pool to max_tx_pool_size with other transactions.
   limit_size loops: next_evict_entry skips A (high evict key).
   Legitimate tx D (fee=5_000 shannons) → rejected Reject::Full.
```
A unit test can assert that after `remove_entry_and_descendants(B)`, `pool_map.get_entry(A).descendants_fee == A.fee` and `pool_map.get_entry(A).descendants_count == 0`.