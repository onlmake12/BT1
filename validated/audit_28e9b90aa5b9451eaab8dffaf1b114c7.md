### Title
Unbounded Descendant Traversal in `remove_entry` Stalls tx-pool Write Lock — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

When a transaction is removed from the tx-pool (e.g., because it was committed in a block), `remove_entry` unconditionally calls `update_descendants_index_key`, which performs an unbounded BFS/DFS traversal over every descendant in the pool and issues an in-memory index mutation (`entries.modify_by_id`) for each one. The number of descendants is not capped by `max_ancestors_count` or any other guard. An unprivileged tx-pool submitter can pre-load a wide descendant fan-out under a single root transaction; when that root is committed in a block, the node performs O(N) index mutations while holding the exclusive tx-pool write lock, stalling all concurrent tx-pool operations for the duration.

---

### Finding Description

**Root cause — `update_descendants_index_key` in `pool_map.rs`**

Every call to `remove_entry` unconditionally invokes both `update_ancestors_index_key` and `update_descendants_index_key`: [1](#0-0) 

`update_descendants_index_key` calls `calc_descendants`, which performs a full BFS over the `TxLinksMap`, collecting every reachable child, grandchild, etc., and then calls `entries.modify_by_id` (a multi-index map mutation) for every entry in that set: [2](#0-1) 

The BFS itself is unbounded — it follows `children` links until the graph is exhausted: [3](#0-2) 

**`max_ancestors_count` does not bound descendants.** It only prevents a new transaction from being admitted if it would have too many ancestors. A single root transaction with many outputs can have an arbitrarily large fan-out of children (each child has only one ancestor — the root — so each passes the check individually): [4](#0-3) 

**`remove_committed_tx` calls `remove_entry` directly**, without the link-pre-removal optimization used by `remove_entry_and_descendants`. This means the full live descendant graph is traversed: [5](#0-4) 

`remove_committed_tx` is called inside `remove_committed_txs`, which is called inside `_update_tx_pool_for_reorg`: [6](#0-5) [7](#0-6) 

The entire reorg update runs under the exclusive tx-pool write lock: [8](#0-7) 

The same direct `remove_entry` call (without link pre-removal) also occurs in `remove_expired`: [9](#0-8) 

**Contrast with `remove_entry_and_descendants`**, which pre-removes all links before iterating, making each subsequent `remove_entry` call O(1) for the descendant traversal: [10](#0-9) 

The comment in that function explicitly acknowledges this design: *"update links state for remove, so that we won't update_descendants_index_key in remove_entry"*. The `remove_committed_tx` path has no equivalent protection.

---

### Impact Explanation

While the tx-pool write lock is held, all concurrent operations are blocked: new transaction submissions, RPC queries, block assembly (`get_block_template`), and orphan resolution. An attacker who pre-loads a root transaction with N descendants causes O(N) `modify_by_id` mutations on every block that commits that root. With a default pool size of tens of megabytes and minimum transaction sizes of ~100–200 bytes, N can reach tens of thousands. Repeated across multiple crafted root transactions committed in successive blocks, this can produce sustained write-lock stalls, degrading or halting tx-pool responsiveness for legitimate users.

---

### Likelihood Explanation

The attacker is an unprivileged tx-pool submitter. They pay fees for T0 and T1…Tn, but the cost is proportional to the number of descendants, while the damage (lock stall duration) scales with the same N. The root transaction T0 is committed by any miner in the normal course of block production — the attacker does not need miner cooperation. The attack is repeatable: after T0 is committed, the attacker submits a new root with a fresh descendant fan-out. The RBF path is separately guarded by `MAX_REPLACEMENT_CANDIDATES = 100`, but the block-commit path has no equivalent descendant count check. [11](#0-10) 

---

### Recommendation

1. **Apply the same link-pre-removal pattern used in `remove_entry_and_descendants` to `remove_committed_tx`.** Before calling `remove_entry` for a committed transaction, remove its links so that `update_descendants_index_key` returns an empty set. Descendants remain in the pool but have their ancestor-weight state corrected lazily or via a separate bounded pass.

2. **Alternatively, cap the descendant fan-out at admission time.** Enforce a `max_descendants_count` symmetric to `max_ancestors_count`, rejecting any transaction whose admission would cause an existing pool entry to exceed the descendant limit.

3. **Decouple descendant weight updates from the hot removal path.** Batch or defer the `modify_by_id` calls for descendants so they do not block the write lock.

---

### Proof of Concept

**Setup:**
- Submit root transaction T0 with 500 outputs to the tx-pool.
- Submit T1, T2, …, T500, each spending one distinct output of T0. Each Ti has exactly 1 ancestor (T0), so all pass `max_ancestors_count`.
- Wait for any miner to commit T0 in a block (normal operation; no miner cooperation required).

**Trigger:**
When the node processes the block containing T0:
1. `_update_tx_pool_for_reorg` acquires the tx-pool write lock.
2. `remove_committed_txs` → `remove_committed_tx(T0)` → `pool_map.remove_entry(T0)`.
3. `update_descendants_index_key(T0, Remove)` calls `links.calc_descendants(T0)` → returns {T1, …, T500}.
4. `entries.modify_by_id` is called 500 times while the write lock is held.

**Amplification:** Submit multiple such root transactions in parallel. Each block commit triggers a separate O(N) stall. With N = 10,000 descendants (achievable within a 20 MB pool with ~200-byte transactions), the stall duration becomes significant enough to time out RPC callers and delay block assembly. [2](#0-1) [5](#0-4) [8](#0-7)

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

**File:** tx-pool/src/component/pool_map.rs (L595-601)
```rust
        let mut ancestors_count = ancestors.len() + 1;
        let mut evicted = Default::default();

        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
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

**File:** tx-pool/src/pool.rs (L223-241)
```rust
    pub(crate) fn remove_committed_txs<'a>(
        &mut self,
        txs: impl Iterator<Item = &'a TransactionView>,
        callbacks: &Callbacks,
        detached_headers: &HashSet<Byte32>,
    ) {
        for tx in txs {
            let tx_hash = tx.hash();
            debug!("try remove_committed_tx {}", tx_hash);
            self.remove_committed_tx(tx, callbacks);

            self.committed_txs_hash_cache
                .put(tx.proposal_short_id(), tx_hash);
        }

        if !detached_headers.is_empty() {
            self.resolve_conflict_header_dep(detached_headers, callbacks)
        }
    }
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

**File:** tx-pool/src/pool.rs (L611-624)
```rust
        // Rule #5, the replaced tx's descendants can not more than 100
        // and the ancestor of the new tx don't have common set with the replaced tx's descendants
        let mut replace_count: usize = 0;
        let mut all_conflicted = conflicts.clone();
        let ancestors = self.pool_map.calc_ancestors(&short_id);
        for conflict in conflicts.iter() {
            let descendants = self.pool_map.calc_descendants(&conflict.id);
            replace_count += descendants.len() + 1;
            if replace_count > MAX_REPLACEMENT_CANDIDATES {
                return Err(Reject::RBFRejected(format!(
                    "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
                    replace_count, MAX_REPLACEMENT_CANDIDATES,
                )));
            }
```

**File:** tx-pool/src/process.rs (L836-851)
```rust
            let mut tx_pool = self.tx_pool.write().await;

            _update_tx_pool_for_reorg(
                &mut tx_pool,
                &attached,
                &detached_headers,
                detached_proposal_id,
                snapshot,
                &self.callbacks,
                mine_mode,
            );

            // notice: readd_detached_tx don't update cache
            self.readd_detached_tx(&mut tx_pool, retain, fetched_cache)
                .await;
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
