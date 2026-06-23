### Title
RBF Conflict Removal Without Rollback on `limit_size` Failure Permanently Drops Old Transactions from Pool - (`tx-pool/src/process.rs`)

---

### Summary

In `submit_entry`, the RBF conflict-removal step (`process_rbf`) executes and **irreversibly evicts** the old conflicting transactions from the pool before the subsequent `limit_size` check can fail. If `limit_size` then evicts the newly inserted replacement transaction (because the pool is full and the new tx has the lowest fee rate), `submit_entry` returns an error — but the old transactions are already permanently gone from the pool. Neither the old nor the new transaction remains in the pool, leaving the user's inputs in an untracked limbo state.

---

### Finding Description

In `tx-pool/src/process.rs`, `submit_entry` executes the following sequence under a single write lock:

```
1. check_rbf(...)                          // validates RBF rules (read-only)
2. process_rbf(tx_pool, &entry, &conflicts) // IRREVERSIBLY removes old txs
3. _submit_entry(tx_pool, ...)             // inserts new tx into pool
4. limit_size(&callbacks, Some(&entry.id)) // may evict the new tx if pool is full
   .map_or(Ok(()), Err)?;                  // propagates eviction as Err
``` [1](#0-0) 

`process_rbf` at step 2 calls `tx_pool.pool_map.remove_entry_and_descendants(id)` for every conflicting transaction, then calls `tx_pool.record_conflict(old.transaction().clone())` to place them in the LRU conflicts cache and fires `call_reject` callbacks marking them `RBFRejected`. [2](#0-1) 

This removal is **not guarded by any rollback**. If `limit_size` at step 4 determines the pool is still over capacity and the newly inserted transaction has the lowest fee rate, it evicts the new tx and returns `Some(Reject::Full(...))`. The `.map_or(Ok(()), Err)?` converts this to an `Err`, causing `submit_entry` to return failure. [3](#0-2) 

At this point:
- The **old conflicting transactions** are permanently removed from the pool (marked `RBFRejected` in the conflicts cache).
- The **new replacement transaction** is also removed (evicted by `limit_size`, marked `Full`).
- The user receives an error response for the new tx submission.
- The user's original pending transaction has silently vanished from the pool.

The codebase itself acknowledges a related missing-rollback problem in a `FIXME` comment in `check_and_record_ancestors`, but dismisses it for the `ExceededMaximumAncestorsCount` path only. The `limit_size` failure path is not covered by that reasoning. [4](#0-3) 

---

### Impact Explanation

A user's pending transaction is silently and permanently removed from the tx-pool without being committed to a block. The user submitted a replacement transaction (RBF), received an error, and now has **neither** the old nor the new transaction in the pool. The user's inputs (cells) remain live on-chain, so funds are not permanently lost, but the user must detect the silent drop and manually resubmit. In a congested network where the pool is consistently full, this can cause repeated silent drops, making it practically impossible for low-fee-rate transactions to remain in the pool once an RBF attempt fails.

---

### Likelihood Explanation

The trigger condition requires:
1. RBF is enabled (`min_rbf_rate > min_fee_rate`).
2. The pool is at or near `max_tx_pool_size`.
3. The new replacement transaction, while meeting the RBF fee minimum over the old tx, has a lower fee rate than the majority of other pool entries.

Condition 3 is realistic: RBF only requires the new tx fee to exceed the old tx fee plus a small delta. If the old tx had a very low fee rate and the pool is full of higher-fee-rate transactions, the new tx (slightly higher than the old) is still the lowest-fee-rate entry and will be evicted by `limit_size`. This is a normal operating condition during network congestion.

---

### Recommendation

Move the `process_rbf` call to **after** `_submit_entry` and `limit_size` both succeed, or implement a rollback path: if `_submit_entry` or `limit_size` returns an error after `process_rbf` has already run, re-insert the removed conflicting transactions back into the pool instead of leaving them in the conflicts cache. The existing `FIXME` comment in `check_and_record_ancestors` already identifies the correct fix direction — validate all failure conditions before performing any irreversible writes. [5](#0-4) 

---

### Proof of Concept

1. Configure a node with RBF enabled (`min_rbf_rate > min_fee_rate`) and a small `max_tx_pool_size`.
2. Fill the pool with many high-fee-rate transactions until it is at capacity.
3. Submit `tx_old` with a low fee rate. It enters the pool, displacing the lowest-fee-rate entry.
4. Submit `tx_new` spending the same inputs as `tx_old`, with a fee rate slightly above `tx_old` (meeting RBF minimum) but still lower than all other pool entries.
5. Inside `submit_entry`:
   - `check_rbf` passes (fee rules satisfied).
   - `process_rbf` removes `tx_old` from the pool; `tx_old` is now `RBFRejected` in the conflicts cache.
   - `_submit_entry` inserts `tx_new` into the pool.
   - `limit_size` finds the pool is still over capacity; `tx_new` is the lowest-fee-rate entry and is evicted; returns `Some(Reject::Full(...))`.
   - `submit_entry` returns `Err(Reject::Full(...))`.
6. Query `get_transaction(tx_old.hash())` → status is `Rejected` (RBFRejected).
7. Query `get_transaction(tx_new.hash())` → status is `Rejected` (Full).
8. Both transactions are gone from the pool. The user's inputs are free but the user received only an error for `tx_new` and no notification about `tx_old`'s removal. [1](#0-0) [6](#0-5)

### Citations

**File:** tx-pool/src/process.rs (L136-152)
```rust
                let may_recovered_txs = self.process_rbf(tx_pool, &entry, &conflicts);
                let evicted = _submit_entry(tx_pool, status, entry.clone(), &self.callbacks)?;

                // in a corner case, a tx with lower fee rate may be rejected immediately
                // after inserting into pool, return proper reject error here
                for evict in evicted {
                    let reject = Reject::Invalidated(format!(
                        "invalidated by tx {}",
                        evict.transaction().hash()
                    ));
                    self.callbacks.call_reject(tx_pool, &evict, reject);
                }

                tx_pool.remove_conflict(&entry.proposal_short_id());
                tx_pool
                    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
                    .map_or(Ok(()), Err)?;
```

**File:** tx-pool/src/process.rs (L203-232)
```rust
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
```

**File:** tx-pool/src/pool.rs (L292-329)
```rust
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
        ret
    }
```

**File:** tx-pool/src/component/pool_map.rs (L582-587)
```rust
    /// Check ancestors and record for entry
    // FIXME: In the scenario that a transaction passed all RBF rules, and then removed the conflicted
    // transaction in txpool, then failed with max ancestor limits, we now need to rollback the removing.
    // this is not an issue currently, because RBF have a rule that not allow any unknown inputs except
    // the conflicted inputs, so the new transaction can not be in a long transaction chain.
    // but it's still safer to report an error before any writing kind of operation.
```
