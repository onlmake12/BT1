### Title
Tx-Pool DoS via Unbounded O(n) Descendant Graph Traversal on Transaction Removal — (File: `tx-pool/src/component/pool_map.rs`)

### Summary
Every call to `remove_entry` in the tx-pool triggers a full BFS traversal of the removed transaction's entire descendant graph via `update_descendants_index_key` → `calc_descendants`. The number of descendants is **not bounded** by `max_ancestors_count` (which only caps ancestor depth at 25). An unprivileged transaction sender can pre-populate the pool with a wide dependency tree, then cause the root to be removed (committed in a block or expired), forcing O(n) CPU work while the tx-pool write lock is held — blocking all concurrent transaction submissions.

---

### Finding Description

**Root cause — `remove_entry` in `pool_map.rs`:** [1](#0-0) 

`remove_entry` unconditionally calls both `update_ancestors_index_key` and `update_descendants_index_key` before removing links: [2](#0-1) 

`update_descendants_index_key` calls `calc_descendants`, which performs an unbounded BFS over the entire child graph: [3](#0-2) 

The BFS visits every reachable descendant and then calls `modify_by_id` on each — O(n) hash-map writes where n = total descendants.

**Why descendants are unbounded:**

`max_ancestors_count` (default 25) limits how many ancestors a single transaction may have, but places **no cap on the number of children** a transaction can have. A transaction with K outputs can have K direct children; each child can have K children of its own; the tree can be 25 levels deep. Total descendants ≈ K^25, bounded only by the 180 MB pool size limit. [4](#0-3) [5](#0-4) 

**Trigger path 1 — committed transaction (no waiting required):**

`remove_committed_tx` calls `remove_entry` directly (not `remove_entry_and_descendants`), so links are still intact when `update_descendants_index_key` runs: [6](#0-5) 

This is called for every transaction in every committed block, inside the tx-pool write lock, via `_update_tx_pool_for_reorg`: [7](#0-6) 

**Trigger path 2 — expiry (12-hour delay):**

`remove_expired` also calls `remove_entry` (not `remove_entry_and_descendants`) for each expired transaction, with links still intact: [8](#0-7) 

**Contrast with the safe path:**

`remove_entry_and_descendants` explicitly strips all links first, so the subsequent `remove_entry` calls find empty ancestor/descendant sets and do O(1) work per node: [9](#0-8) 

`remove_entry` called directly does not benefit from this optimization.

---

### Impact Explanation

The tx-pool is a single-threaded service protected by a write lock. While `update_descendants_index_key` traverses n descendants, all concurrent operations — `send_transaction` RPC calls, relay submissions, block assembly — are blocked. With the 180 MB pool limit and ~100-byte minimum transaction size, n can reach ~1.8 million entries. At that scale the BFS + hash-map writes can consume hundreds of milliseconds per committed transaction, stalling the pool. An attacker who pre-fills the pool and then has the root committed (or waits for expiry) can repeatedly trigger this stall, degrading or denying service to legitimate users.

---

### Likelihood Explanation

The attacker entry path is the standard `send_transaction` RPC or P2P relay — no special privilege required. The minimum fee rate is 1,000 shannons/KB; a minimal transaction (~100 bytes) costs ~100 shannons. Filling the pool with 1.8 million transactions costs roughly 180 billion shannons (~1.8 CKB in fees). The attacker also needs sufficient CKB capacity for outputs, which raises the capital cost, but the fee cost alone is low. Having the root transaction committed requires only that a miner includes it — standard behavior for any fee-paying transaction. The attack is therefore realistic for a motivated adversary.

---

### Recommendation

1. **Cap descendants per transaction** analogously to `max_ancestors_count`. Reject or evict transactions that would push any ancestor's descendant count above a configurable limit.
2. **Use `remove_entry_and_descendants` in `remove_committed_tx` and `remove_expired`** (stripping links first) so that descendant-weight updates are skipped for already-removed nodes, reducing per-removal cost to O(1).
3. **Lazy descendant-weight updates**: store a "generation counter" on each entry and recompute weights on demand rather than eagerly propagating on every removal.

---

### Proof of Concept

```
# Attacker constructs a wide dependency tree (depth d, fan-out K):
# T0 has K outputs → K children T1_1..T1_K
# Each T1_i has K outputs → K² grandchildren T2_1..T2_K²
# ...up to depth d ≤ 25 (max_ancestors_count)
# Total descendants ≈ K^d, bounded by pool size (~1.8M txs at 180 MB)

1. Submit T0 (K outputs) via send_transaction RPC.
2. Submit K transactions T1_1..T1_K, each spending one output of T0.
3. Submit K² transactions T2_1..T2_K², each spending one output of a T1_i.
4. Continue until pool approaches 180 MB.
5. T0 is included in a block by any miner.
6. Node calls remove_committed_tx(T0)
     → pool_map.remove_entry(T0)
       → update_descendants_index_key(T0)
         → calc_descendants(T0)  [BFS over ~K^d nodes]
         → modify_by_id(desc_id) for each descendant
   All executed under the tx-pool write lock.
7. All concurrent send_transaction calls stall for the duration.
8. Repeat as subsequent levels of the tree are committed in later blocks.
```

**Relevant code locations:**

| Location | Role |
|---|---|
| `tx-pool/src/component/pool_map.rs:235–250` | `remove_entry` — calls both update functions |
| `tx-pool/src/component/pool_map.rs:447–460` | `update_descendants_index_key` — O(n) traversal |
| `tx-pool/src/component/links.rs:52–72` | `calc_relation_ids` — BFS implementation |
| `tx-pool/src/pool.rs:253–268` | `remove_committed_tx` — trigger path 1 |
| `tx-pool/src/pool.rs:270–288` | `remove_expired` — trigger path 2 |

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

**File:** util/app-config/src/configs/tx_pool.rs (L25-26)
```rust
    /// max ancestors size limit for a single tx
    pub max_ancestors_count: usize,
```

**File:** resource/ckb.toml (L215-216)
```text
max_tx_verify_cycles = 70_000_000
max_ancestors_count = 25
```

**File:** tx-pool/src/pool.rs (L253-268)
```rust
    fn remove_committed_tx(&mut self, tx: &TransactionView, callbacks: &Callbacks) {
        let short_id = tx.proposal_short_id();
        if let Some(_entry) = self.pool_map.remove_entry(&short_id) {
            debug!("remove_committed_tx for {}", tx.hash());
        }
        {
            for (entry, reject) in self.pool_map.resolve_conflict(tx) {
                debug!(
                    "removed {} for committed: {}",
                    entry.transaction().hash(),
                    tx.hash()
                );
                callbacks.call_reject(self, &entry, reject);
            }
        }
    }
```

**File:** tx-pool/src/pool.rs (L270-288)
```rust
    // Expire all transaction (and their dependencies) in the pool.
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

**File:** tx-pool/src/process.rs (L1039-1056)
```rust
fn _update_tx_pool_for_reorg(
    tx_pool: &mut TxPool,
    attached: &LinkedHashSet<TransactionView>,
    detached_headers: &HashSet<Byte32>,
    detached_proposal_id: HashSet<ProposalShortId>,
    snapshot: Arc<Snapshot>,
    callbacks: &Callbacks,
    mine_mode: bool,
) {
    tx_pool.snapshot = Arc::clone(&snapshot);

    // NOTE: `remove_by_detached_proposal` will try to re-put the given expired/detached proposals into
    // pending-pool if they can be found within txpool. As for a transaction
    // which is both expired and committed at the one time(commit at its end of commit-window),
    // we should treat it as a committed and not re-put into pending-pool. So we should ensure
    // that involves `remove_committed_txs` before `remove_expired`.
    tx_pool.remove_committed_txs(attached.iter(), callbacks, detached_headers);
    tx_pool.remove_by_detached_proposal(detached_proposal_id.iter());
```
