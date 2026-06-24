All cited code references verified against the actual source. Here is the validated finding:

---

Audit Report

## Title
O(N²) Descendant Traversal in `remove_expired` via Sequential `remove_entry` Without Pre-Removing Links — (`tx-pool/src/component/pool_map.rs`, `tx-pool/src/pool.rs`)

## Summary

`remove_expired` in `pool.rs` collects all expired transactions and removes them one-by-one via `remove_entry`. Each call to `remove_entry` invokes `update_descendants_index_key`, which performs a full BFS over all remaining descendants via `calc_descendants` while the link graph is still intact. For a chain of N expired transactions removed root-first, this produces O(N²) total BFS work, all executed while holding the tx-pool write lock.

## Finding Description

**Root cause confirmed in source:**

`remove_entry` at [1](#0-0)  calls `update_descendants_index_key` at line 243 and only then calls `remove_entry_links` at line 245. The link graph is fully intact during the BFS.

`update_descendants_index_key` at [2](#0-1)  calls `self.links.calc_descendants()`, which delegates to `calc_relative_ids` → `calc_relation_ids`.

`calc_relation_ids` at [3](#0-2)  is an unbounded BFS with no iteration cap.

`remove_expired` at [4](#0-3)  collects all expired entries and calls `pool_map.remove_entry` sequentially, with no pre-removal of links.

**The existing optimization is present but unused here:** `remove_entry_and_descendants` at [5](#0-4)  explicitly pre-removes all links for all targeted entries before calling `remove_entry`, with a comment confirming this prevents the redundant `update_descendants_index_key` traversal. `remove_expired` does not use this function.

**Exploit flow:** For a chain of N transactions (tx₀ → tx₁ → … → tx_{N-1}) all expiring simultaneously and removed root-first: removing tx₀ traverses N−1 descendants; removing tx₁ traverses N−2; total = (N−1)+(N−2)+…+1 = O(N²/2). The `MultiIndexMap` iteration order is not guaranteed leaf-first, making root-first ordering a realistic scenario.

`remove_expired` is called from `_update_tx_pool_for_reorg` on every block: [6](#0-5) 

Default configuration confirms attack parameters: [7](#0-6) 

## Impact Explanation

During the O(N²) traversal, the tx-pool write lock is held continuously. No new transactions can be submitted, no block templates can be generated, and all write-path RPC calls queue or time out. The node does not crash and recovers after the stall completes. This matches **Low (501–2000 points) — important performance improvement for CKB**. The "High: network congestion with few costs" threshold is not met because the attack requires substantial fee expenditure to fill the pool with chained transactions.

## Likelihood Explanation

Low. The attacker must fund and submit up to ~900,000 chained transactions (paying fees for each), wait the full 12-hour expiry window, and the effect triggers only once per expiry cycle. The pool self-limits at 180 MB (`DEFAULT_MAX_TX_POOL_SIZE`), capping the maximum chain count. No miner cooperation or privileged access is required — any `send_transaction` RPC caller can execute this — but the economic cost is significant.

## Recommendation

1. **Apply the `remove_entry_and_descendants` pattern in `remove_expired`**: Before the removal loop, pre-remove all links for all expired entries so that `update_descendants_index_key` finds an empty descendant set on each subsequent `remove_entry` call, reducing total work to O(N).
2. **Alternatively, batch removals**: Remove expired transactions in bounded batches per block (e.g., ≤256 per block) to amortize the work across multiple block events.
3. **Guard in `update_descendants_index_key`**: Skip the update if the descendant count exceeds a configurable threshold, relying on lazy recomputation.

## Proof of Concept

1. Configure a CKB node with defaults (`max_ancestors_count=1000`, `expiry_hours=12`, `max_tx_pool_size=180_000_000`).
2. Submit a root transaction spending a confirmed UTXO, then 999 child transactions forming a linear chain (each spending the previous output). Repeat for as many chains as the pool allows (~900 chains, ~900,000 total transactions).
3. Wait 12 hours for all transactions to reach expiry.
4. When the next block arrives, `_update_tx_pool_for_reorg` → `remove_expired` is triggered.
5. `remove_expired` collects all ~900,000 expired entries and calls `remove_entry` sequentially. For each chain removed root-first, `update_descendants_index_key` → `calc_descendants` traverses 999+998+…+1 = 499,500 nodes per chain; total ≈ 900 × 499,500 ≈ 450,000,000 BFS steps under the write lock.
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

**File:** tx-pool/src/process.rs (L1109-1113)
```rust
    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
```

**File:** util/app-config/src/legacy/tx_pool.rs (L16-20)
```rust
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
// Default expiration time for pool transactions in hours
const DEFAULT_EXPIRY_HOURS: u8 = 12;
// Default max_tx_pool_size 180mb
const DEFAULT_MAX_TX_POOL_SIZE: usize = 180_000_000;
```
