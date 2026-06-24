All code references have been verified against the actual source. Here is the validation result:

Audit Report

## Title
Stale `descendants_*` Accumulator Fields After `remove_entry_and_descendants` Enables Tx-Pool Eviction Manipulation — (`tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::remove_entry_and_descendants` pre-removes all link graph entries via `remove_entry_links` before invoking `remove_entry` on each subtree member. Because `update_ancestors_index_key` resolves ancestors through the live link graph, the pre-removal causes it to return an empty ancestor set, leaving every out-of-subtree ancestor with permanently inflated `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee`. These stale values propagate into each ancestor's `EvictKey`, causing the eviction subsystem to systematically skip those ancestors and instead evict legitimate higher-fee transactions.

## Finding Description

**Root cause**

`remove_entry_and_descendants` (lines 252–265) pre-removes all link entries before calling `remove_entry`: [1](#0-0) 

The comment on line 256 reveals the intent: pre-removing links was meant to suppress redundant `update_descendants_index_key` calls (since all descendants are being removed anyway). However, this optimization has an unintended side effect on `update_ancestors_index_key`.

When `remove_entry` is subsequently called (lines 235–250), it invokes both `update_ancestors_index_key` and `update_descendants_index_key`: [2](#0-1) 

`update_ancestors_index_key` calls `self.links.calc_ancestors(&child.proposal_short_id())`: [3](#0-2) 

`calc_ancestors` delegates to `calc_relative_ids`, which does `self.inner.get(short_id)`: [4](#0-3) 

Since `remove_entry_links` already called `self.links.remove(id)` (line 429), removing the entry from `self.links.inner`: [5](#0-4) 

…`self.inner.get(short_id)` returns `None`, and `unwrap_or_default()` yields an empty `HashSet`. The `for anc_id in &ancestors` loop in `update_ancestors_index_key` never executes, so `sub_descendant_weight` is never called on any out-of-subtree ancestor.

**Where stale values are consumed**

`EvictKey` is computed from `descendants_fee`, `descendants_size`, and `descendants_cycles`: [6](#0-5) 

`EvictKey` is ordered ascending by `fee_rate` first, then `descendants_count`: [7](#0-6) 

`next_evict_entry` picks the first (lowest) entry from `iter_by_evict_key()`: [8](#0-7) 

An ancestor with inflated `descendants_fee` has an artificially high `descendants_feerate`, making `fee_rate = descendants_feerate.max(feerate)` larger than the entry's true fee rate. It is never the minimum in the evict key ordering and is systematically skipped. The stale `descendants_count` field further reinforces this, as it is the secondary sort key.

**Trigger path**

`resolve_conflict` is called on every conflicting transaction submission and directly calls `remove_entry_and_descendants`: [9](#0-8) 

This path is reachable by any unprivileged caller via `send_transaction` RPC or P2P relay.

## Impact Explanation

An ancestor entry with inflated `descendants_*` fields evades eviction indefinitely. When the pool reaches its size limit, `limit_size` repeatedly skips the ancestor and evicts legitimate higher-fee transactions instead. The stale fields are never corrected by any background task; only `clear()` (line 387) or a full reorg resets them. If exploited at scale, the pool fills with low-fee transactions that cannot be evicted, causing **CKB network congestion with few costs** — matching the **High (10001–15000 points)** impact class.

## Likelihood Explanation

The trigger is cheap and requires no privileged access:
1. Submit parent tx **A** (low fee, above `min_fee_rate`).
2. Submit high-fee child **B** spending A's output. A's `descendants_*` fields are updated upward.
3. Submit conflicting tx **B'** (RBF-eligible, marginally higher fee). `resolve_conflict` → `remove_entry_and_descendants(B)` → A's `descendants_*` fields are **not** decremented.
4. Repeat steps 2–3 to keep the illusion alive.

The attacker pays only the marginal RBF fee bump per cycle. A single RPC connection suffices; no Sybil attack, no majority hash power, and no victim mistakes are required. The bug is deterministically reproducible.

## Recommendation

Before tearing down the link graph in `remove_entry_and_descendants`, update the ancestors of the root entry while the graph is still intact. Concretely, retrieve the root entry and call `update_ancestors_index_key(root_entry, EntryOp::Remove)` **before** the `remove_entry_links` loop. Only then proceed to strip links and remove entries. This ensures `calc_ancestors` can still traverse the live graph and correctly decrement every out-of-subtree ancestor's `descendants_*` accumulators and `EvictKey`.

## Proof of Concept

```
Initial state:
  A (low fee) → B (high fee) → C (high fee)
  A.descendants_fee   = fee_A + fee_B + fee_C
  A.descendants_count = 3
  A.evict_key.fee_rate = descendants_feerate(inflated)

Step 1: Submit B' conflicting with B (RBF, fee_B' > fee_B)
  → resolve_conflict(B') calls remove_entry_and_descendants(B)
    removed_ids = [B, C]
    remove_entry_links(B)  ← B removed from links.inner; A.children no longer contains B
    remove_entry_links(C)  ← C removed from links.inner
    remove_entry(B):
      update_ancestors_index_key(B, Remove)
        calc_ancestors(B) → links.inner.get(B) = None → returns {}
        → A.sub_descendant_weight(B) NEVER CALLED
    remove_entry(C): similarly no-op

State after:
  A still in pool
  A.descendants_fee   = fee_A + fee_B + fee_C  ← STALE (should be fee_A)
  A.descendants_count = 3                       ← STALE (should be 1)
  A.evict_key.fee_rate = descendants_feerate(inflated) > A.own_feerate
  → A is never selected by next_evict_entry even when pool is over limit

Step 2: Repeat — submit B again, then B' again — stale values accumulate further.
```

A unit test can confirm this by asserting `A.descendants_count == 1` and `A.descendants_fee == fee_A` after the conflict resolution, which will fail against the current code.

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

**File:** tx-pool/src/component/pool_map.rs (L432-434)
```rust
    fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
        let ancestors: HashSet<ProposalShortId> =
            self.links.calc_ancestors(&child.proposal_short_id());
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
