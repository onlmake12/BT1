### Title
`remove_expired` Skips Descendant Cascade, Leaving Child Transactions Permanently Stuck in Tx-Pool — (`tx-pool/src/pool.rs`)

### Summary

`TxPool::remove_expired` removes only the single expired entry from the pool map, while `TxPool::limit_size` correctly uses `remove_entry_and_descendants`. When a parent transaction expires, its in-pool children are left with dangling inputs that can never be resolved, mirroring the lending-pool bug where a Late→Expired transition had no handler to unlock capital.

### Finding Description

`remove_expired` iterates over every pool entry, collects those whose `timestamp + expiry < now`, and removes each with `pool_map.remove_entry(short_id)`: [1](#0-0) 

`remove_entry` removes only the targeted entry and its edges. By contrast, `limit_size` — the only other bulk-removal path — calls `pool_map.remove_entry_and_descendants(&id)`: [2](#0-1) 

The asymmetry means that when a parent transaction expires:

1. The parent is removed and its consumed UTXOs are released.
2. Every child transaction that references the parent's outputs remains in the pool (`Pending`, `Gap`, or `Proposed`).
3. Those children's inputs now point to outputs that are neither on-chain nor in the pool — they are permanently unresolvable.
4. The children will never be committed and will occupy pool space until their own timestamps also cross the expiry threshold (or until `limit_size` evicts them by fee-rate, which is non-deterministic).

`_update_tx_pool_for_reorg` calls `remove_expired` on every new block, so the window for this to occur is every block interval: [3](#0-2) 

The existing comment in `_update_tx_pool_for_reorg` already acknowledges that ordering between removal passes matters (committed before expired), but it does not address the missing descendant cascade inside `remove_expired` itself: [4](#0-3) 

The pool `Status` enum (`Pending`, `Gap`, `Proposed`) is the state machine: [5](#0-4) 

There is no transition handler for the case where a parent moves from any of those states to "expired-and-removed" while its children remain in a live state — exactly the missing Late→Expired handler in the reference report.

### Impact Explanation

- **Stuck transactions**: Child transactions remain in the pool indefinitely with unresolvable inputs. They are counted in pool statistics, consume memory, and can never be mined.
- **Pool exhaustion / DoS**: An unprivileged submitter can craft a parent + N children chain, let the parent expire, and repeat. Each cycle leaves N children permanently occupying pool slots until their own expiry. With a large enough fan-out or repeated submissions, the pool fills with garbage entries, causing legitimate transactions to be rejected by `limit_size` eviction.
- **Incorrect pool metrics**: `pending_count`, `gap_count`, and `proposed_count` remain inflated, misleading operators and the block assembler.

### Likelihood Explanation

- Any tx-pool submitter (RPC `send_transaction`) can trigger this without any privilege.
- The condition is met whenever a parent transaction's `timestamp + expiry` elapses before its children's timestamps do — a natural occurrence when a user submits a parent first and children later, or when the parent is submitted just before the expiry window closes.
- `remove_expired` is called on every block via `_update_tx_pool_for_reorg`, so the trigger fires continuously.

### Recommendation

Replace `pool_map.remove_entry` with `pool_map.remove_entry_and_descendants` inside `remove_expired`, matching the behavior of `limit_size`. Alternatively, collect the full closure of expired entries (parent + all transitive descendants) before removal, and fire `call_reject` for each.

```rust
// current (broken)
self.pool_map.remove_entry(&entry.proposal_short_id());

// fix
let removed = self.pool_map.remove_entry_and_descendants(&entry.proposal_short_id());
for e in removed {
    callbacks.call_reject(self, &e, Reject::Expiry(e.timestamp));
}
```

### Proof of Concept

1. Submit parent tx `P` to the node via RPC `send_transaction`. Record its `timestamp` (pool entry time).
2. Immediately submit child tx `C` spending an output of `P`. `C` gets a slightly later `timestamp`.
3. Wait for `expiry` milliseconds past `P`'s timestamp (default pool expiry window).
4. Mine a new block. `_update_tx_pool_for_reorg` → `remove_expired` fires.
5. `P` is removed (its timestamp crossed expiry). `C` is **not** removed (its timestamp has not yet crossed expiry).
6. Query `get_transaction(C.hash())`: status is still `Pending` or `Proposed`.
7. Attempt to mine a block including `C`: block assembly fails to resolve `C`'s input (parent output gone).
8. `C` remains in the pool consuming space. Repeat steps 1–5 with many children to exhaust pool capacity. [1](#0-0) [6](#0-5)

### Citations

**File:** tx-pool/src/pool.rs (L271-287)
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
```

**File:** tx-pool/src/pool.rs (L290-327)
```rust
    // Remove transactions from the pool until total size <= size_limit.
    // Return a `Reject` for current inserting entry if it's removed
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
```

**File:** tx-pool/src/process.rs (L1050-1056)
```rust
    // NOTE: `remove_by_detached_proposal` will try to re-put the given expired/detached proposals into
    // pending-pool if they can be found within txpool. As for a transaction
    // which is both expired and committed at the one time(commit at its end of commit-window),
    // we should treat it as a committed and not re-put into pending-pool. So we should ensure
    // that involves `remove_committed_txs` before `remove_expired`.
    tx_pool.remove_committed_txs(attached.iter(), callbacks, detached_headers);
    tx_pool.remove_by_detached_proposal(detached_proposal_id.iter());
```

**File:** tx-pool/src/process.rs (L1109-1110)
```rust
    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);
```

**File:** tx-pool/src/component/pool_map.rs (L23-28)
```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Status {
    Pending,
    Gap,
    Proposed,
}
```
