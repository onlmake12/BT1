I have all the code I need to validate this claim. Let me verify the key assertions against the actual source.

Audit Report

## Title
O(N²) Descendant Traversal in `remove_expired` via `remove_entry` Without Pre-Removing Links — (`tx-pool/src/component/pool_map.rs`, `tx-pool/src/pool.rs`)

## Summary

`remove_expired` calls `remove_entry` sequentially for each expired transaction without pre-removing link graph edges. Because `remove_entry` invokes `update_descendants_index_key` — which performs a full BFS over all remaining descendants — before calling `remove_entry_links`, removing a chain of N expired transactions incurs O(N²) total descendant traversal work. This holds the tx-pool write lock for the entire duration, blocking all concurrent tx-pool operations during block processing.

## Finding Description

**Root cause:** `remove_entry` in `pool_map.rs` calls `update_descendants_index_key` at L243 and only then calls `remove_entry_links` at L245, meaning the link graph is fully intact during the BFS traversal. [1](#0-0) 

`update_descendants_index_key` calls `self.links.calc_descendants()`, which is an unbounded BFS with no iteration cap: [2](#0-1) [3](#0-2) 

`remove_expired` collects all expired entries and removes them one-by-one via `remove_entry`, without pre-removing links: [4](#0-3) 

The existing optimization is present in `remove_entry_and_descendants`, which explicitly pre-removes all links before calling `remove_entry` (with a comment noting this prevents the redundant traversal), but `remove_expired` does not use this function: [5](#0-4) 

**Exploit flow:** For a chain of N transactions (tx₀ → tx₁ → … → tx_{N-1}) all expiring simultaneously, if removed root-first: removing tx₀ traverses N−1 descendants; removing tx₁ traverses N−2 (tx₀'s link was cleaned up but tx₁ still has N−2 children); total = (N−1)+(N−2)+…+1 = O(N²/2). The worst-case order depends on `MultiIndexMap` iteration order, which is not guaranteed to be leaf-first.

`remove_expired` is called from `_update_tx_pool_for_reorg` on every block while holding the tx-pool write lock: [6](#0-5) 

Default configuration confirms the attack parameters: [7](#0-6) 

## Impact Explanation

During the O(N²) work, the tx-pool write lock is held continuously. No new transactions can be submitted, no block templates can be generated, and all write-path RPC calls queue or time out. The node does not crash and recovers after the stall completes. This constitutes an important performance degradation matching the allowed impact: **Low (501–2000 points) — important performance improvement for CKB**. The "High: network congestion with few costs" threshold is not met because the attack requires substantial fee expenditure.

## Likelihood Explanation

Low. The attacker must fund and submit up to ~900,000 chained transactions (paying fees for each), wait the full 12-hour expiry window, and the effect triggers only once per expiry cycle. The pool self-limits at 180 MB, capping the maximum chain count. No miner cooperation or privileged access is required — any `send_transaction` RPC caller can execute this — but the economic cost is significant.

## Recommendation

1. **Use `remove_entry_and_descendants` pattern in `remove_expired`**: Pre-remove all links for all expired entries before the removal loop, so `update_descendants_index_key` finds an empty descendant set on each call. This is exactly the optimization already present in `remove_entry_and_descendants`.
2. **Alternatively, batch removals**: Remove expired transactions in bounded batches per block (e.g., ≤256 per block) to amortize the work across multiple block events.
3. **Guard in `update_descendants_index_key`**: Skip the update if the descendant count exceeds a threshold, relying on lazy recomputation.

## Proof of Concept

1. Configure a CKB node with defaults (`max_ancestors_count=1000`, `expiry_hours=12`, `max_tx_pool_size=180_000_000`).
2. Submit a root transaction spending a confirmed UTXO, then 999 child transactions forming a linear chain (each spending the previous output). Repeat for as many chains as the pool allows (~900 chains).
3. Wait 12 hours for all transactions to reach expiry.
4. When the next block arrives, `_update_tx_pool_for_reorg` → `remove_expired` is triggered.
5. `remove_expired` collects all ~900,000 expired entries and calls `remove_entry` sequentially. For each chain, if removed root-first, `update_descendants_index_key` → `calc_descendants` traverses 999+998+…+1 = 499,500 nodes per chain; total ≈ 900 × 499,500 ≈ 450,000,000 BFS steps under the write lock.
6. **Observable effect**: `send_transaction`, `get_block_template`, and all write-path RPCs stall or time out for the duration of the traversal. The node recovers after the work completes.

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

**File:** tx-pool/src/component/pool_map.rs (L447-460)
```rust
    fn update_descendants_index_key(&mut self, parent: &TxEntry, op: EntryOp) {
        let descendants: HashSet<ProposalShortId> =
            self.links.calc_descendants(&parent.proposal_short_id());
        for desc_id in &descendants {
            // update child score
            self.entries.modify_by_id(desc_id, |e| {
                match op {
                    EntryOp::Remove => e.inner.sub_ancestor_weight(parent),
                    EntryOp::Add => e.inner.add_ancestor_weight(parent),
                };
                e.score = e.inner.as_score_key();
            });
        }
    }
```

**File:** tx-pool/src/component/links.rs (L52-72)
```rust
    pub fn calc_relation_ids(
        &self,
        mut stage: HashSet<ProposalShortId>,
        relation: Relation,
    ) -> HashSet<ProposalShortId> {
        let mut relation_ids = HashSet::with_capacity(stage.len());

        while let Some(id) = stage.iter().next().cloned() {
            //recursively
            if let Some(tx_links) = self.inner.get(&id) {
                for direct_id in tx_links.get_direct_ids(relation) {
                    if !relation_ids.contains(direct_id) {
                        stage.insert(direct_id.clone());
                    }
                }
            }
            stage.remove(&id);
            relation_ids.insert(id);
        }
        relation_ids
    }
```

**File:** tx-pool/src/pool.rs (L271-288)
```rust
    pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
        let now_ms = ckb_systemtime::unix_time_as_millis();

        let removed: Vec<_> = self
            .pool_map
            .iter()
            .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
            .map(|entry| entry.inner.clone())
            .collect();

        for entry in removed {
            let tx_hash = entry.transaction().hash();
            debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
            self.pool_map.remove_entry(&entry.proposal_short_id());
            let reject = Reject::Expiry(entry.timestamp);
            callbacks.call_reject(self, &entry, reject);
        }
    }
```

**File:** tx-pool/src/process.rs (L1109-1114)
```rust
    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
}
```

**File:** util/app-config/src/legacy/tx_pool.rs (L16-20)
```rust
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
// Default expiration time for pool transactions in hours
const DEFAULT_EXPIRY_HOURS: u8 = 12;
// Default max_tx_pool_size 180mb
const DEFAULT_MAX_TX_POOL_SIZE: usize = 180_000_000;
```
