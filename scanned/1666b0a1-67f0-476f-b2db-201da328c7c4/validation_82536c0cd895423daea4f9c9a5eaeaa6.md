### Title
Conflict Cache Entry Overwrite Silently Drops Recoverable Transactions - (File: tx-pool/src/pool.rs)

### Summary
In `record_conflict`, the `conflicts_outputs_cache` maps each input `OutPoint` to a single `ProposalShortId`. When two different rejected/conflicting transactions share the same input outpoint, the second `record_conflict` call unconditionally overwrites the first transaction's entry. The first transaction is then permanently unreachable via `get_conflicted_txs_from_inputs`, so it is never re-queued for verification after an RBF replacement clears the conflict — mirroring the "Balance Entry Overwrite" pattern exactly.

### Finding Description
`record_conflict` iterates every input of the rejected transaction and calls `lru::LruCache::put`, which always overwrites:

```rust
// tx-pool/src/pool.rs  (record_conflict)
pub(crate) fn record_conflict(&mut self, tx: TransactionView) {
    let short_id = tx.proposal_short_id();
    for inputs in tx.input_pts_iter() {
        self.conflicts_outputs_cache.put(inputs, short_id.clone()); // ← unconditional overwrite
    }
    self.conflicts_cache.put(short_id.clone(), tx);
    ...
}
``` [1](#0-0) 

The reverse-lookup cache `conflicts_outputs_cache: lru::LruCache<OutPoint, ProposalShortId>` therefore stores **at most one** `ProposalShortId` per `OutPoint`. When a second conflicting transaction T2 that spends the same `OutPoint O` is recorded, `conflicts_outputs_cache[O]` is silently overwritten with T2's id, and T1's id is lost for that slot.

The recovery path in `process_rbf` calls `get_conflicted_txs_from_inputs` to find transactions that may become valid again after the conflicting pool entry is removed:

```rust
may_recovered_txs = tx_pool.get_conflicted_txs_from_inputs(available_inputs.into_iter());
``` [2](#0-1) 

`get_conflicted_txs_from_inputs` performs a two-step lookup — `conflicts_outputs_cache` → `conflicts_cache`:

```rust
pub(crate) fn get_conflicted_txs_from_inputs(...) -> Vec<TransactionView> {
    inputs
        .filter_map(|input| {
            self.conflicts_outputs_cache
                .peek(&input)
                .and_then(|id| self.conflicts_cache.peek(id).cloned())
        })
        .collect()
}
``` [3](#0-2) 

Because `conflicts_outputs_cache[O]` now points only to T2, T1 is never returned and never re-queued, even though T1 is still present in `conflicts_cache` and may be perfectly valid after the RBF replacement.

### Impact Explanation
Any transaction T1 that was rejected as a conflict and whose input outpoint is later overwritten in `conflicts_outputs_cache` by a second conflicting transaction T2 will **never be automatically recovered** after an RBF replacement removes the original conflicting pool entry. T1 is silently dropped from the automatic re-verification pipeline. The user must detect the omission and resubmit T1 manually. In time-sensitive scenarios (e.g., a transaction that must land in a specific epoch window), this silent drop can cause the transaction to miss its validity window entirely.

### Likelihood Explanation
The scenario is reachable by any unprivileged RPC caller or relay peer. An attacker who observes a pending pool transaction spending `OutPoint O` can submit two cheap transactions T1 and T2 that both spend O. Both are rejected and recorded as conflicts. T2's `record_conflict` call overwrites T1's slot in `conflicts_outputs_cache`. When the original pool transaction is later replaced via RBF, only T2 is recovered; T1 is silently discarded. No special privilege, key material, or majority hash-power is required.

### Recommendation
Replace the unconditional `put` with a conditional insert that preserves the first-recorded entry, or change the value type to `Vec<ProposalShortId>` / `HashSet<ProposalShortId>` so that all conflicting transactions for a given outpoint are tracked:

```rust
// Option A – keep first entry (matches the "do not overwrite" fix pattern)
self.conflicts_outputs_cache
    .get_or_insert(inputs, || short_id.clone());

// Option B – track all conflicting ids per outpoint
// conflicts_outputs_cache: HashMap<OutPoint, Vec<ProposalShortId>>
self.conflicts_outputs_cache
    .entry(inputs)
    .or_default()
    .push(short_id.clone());
```

`get_conflicted_txs_from_inputs` and `remove_conflict` must be updated accordingly.

### Proof of Concept
1. Pool contains transaction P spending `OutPoint O`.
2. Attacker submits T1 (spends O, different outputs). T1 is rejected (`Dead` outpoint), `record_conflict(T1)` sets `conflicts_outputs_cache[O] = T1.id`.
3. Attacker submits T2 (also spends O). T2 is rejected, `record_conflict(T2)` sets `conflicts_outputs_cache[O] = T2.id`, **overwriting T1's entry**.
4. A third party submits T3 that replaces P via RBF. `process_rbf` removes P, collects P's inputs (`{O}`), calls `get_conflicted_txs_from_inputs({O})`.
5. The lookup returns only T2 (via `conflicts_outputs_cache[O] = T2.id`). T1 is never returned.
6. T2 is re-queued for verification; T1 is permanently dropped from the automatic recovery pipeline despite still residing in `conflicts_cache`. [4](#0-3) [1](#0-0) [5](#0-4)

### Citations

**File:** tx-pool/src/pool.rs (L48-51)
```rust
    pub(crate) conflicts_cache: lru::LruCache<ProposalShortId, TransactionView>,
    // conflicted transaction outputs cache, input -> tx_short_id
    pub(crate) conflicts_outputs_cache: lru::LruCache<OutPoint, ProposalShortId>,
}
```

**File:** tx-pool/src/pool.rs (L164-175)
```rust
    pub(crate) fn record_conflict(&mut self, tx: TransactionView) {
        let short_id = tx.proposal_short_id();
        for inputs in tx.input_pts_iter() {
            self.conflicts_outputs_cache.put(inputs, short_id.clone());
        }
        self.conflicts_cache.put(short_id.clone(), tx);
        debug!(
            "record_conflict {:?} now cache size: {}",
            short_id,
            self.conflicts_cache.len()
        );
    }
```

**File:** tx-pool/src/pool.rs (L190-201)
```rust
    pub(crate) fn get_conflicted_txs_from_inputs(
        &self,
        inputs: impl Iterator<Item = OutPoint>,
    ) -> Vec<TransactionView> {
        inputs
            .filter_map(|input| {
                self.conflicts_outputs_cache
                    .peek(&input)
                    .and_then(|id| self.conflicts_cache.peek(id).cloned())
            })
            .collect()
    }
```

**File:** tx-pool/src/process.rs (L196-234)
```rust
        let mut may_recovered_txs = vec![];
        let mut available_inputs = HashSet::new();

        if conflicts.is_empty() {
            return may_recovered_txs;
        }

        let all_removed: Vec<_> = conflicts
            .iter()
            .flat_map(|id| tx_pool.pool_map.remove_entry_and_descendants(id))
            .collect();

        available_inputs.extend(
            all_removed
                .iter()
                .flat_map(|removed| removed.transaction().input_pts_iter()),
        );

        for input in entry.transaction().input_pts_iter() {
            available_inputs.remove(&input);
        }

        may_recovered_txs = tx_pool.get_conflicted_txs_from_inputs(available_inputs.into_iter());
        for old in all_removed {
            debug!(
                "remove conflict tx {} for RBF by new tx {}",
                old.transaction().hash(),
                entry.transaction().hash()
            );
            let reject =
                Reject::RBFRejected(format!("replaced by tx {}", entry.transaction().hash()));

            // RBF replace successfully, put old transactions into conflicts pool
            tx_pool.record_conflict(old.transaction().clone());
            // after removing old tx from tx_pool, we call reject callbacks manually
            self.callbacks.call_reject(tx_pool, &old, reject);
        }
        assert!(!may_recovered_txs.contains(entry.transaction()));
        may_recovered_txs
```
