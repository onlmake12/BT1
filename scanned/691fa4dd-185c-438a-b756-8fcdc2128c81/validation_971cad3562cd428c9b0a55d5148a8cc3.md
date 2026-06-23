### Title
RBF Partial-Removal Without Rollback Leaves Tx-Pool in Inconsistent State — (`File: tx-pool/src/component/pool_map.rs`, `tx-pool/src/process.rs`)

---

### Summary

In CKB's Replace-By-Fee (RBF) tx-pool logic, conflicted transactions are irreversibly removed from the pool **before** the replacement transaction is confirmed to be successfully inserted. If the insertion step subsequently fails, the pool is left in an inconsistent state: the original transactions are gone but the replacement is also absent. The developers explicitly acknowledge this in a `FIXME` comment but claim it is currently unreachable via one specific failure path. A second failure path (`limit_size`) is not addressed by that mitigation.

---

### Finding Description

In `tx-pool/src/process.rs`, the `submit_entry` function executes the following sequence inside a write lock:

```rust
let may_recovered_txs = self.process_rbf(tx_pool, &entry, &conflicts);   // (1) removes conflicted txs
let evicted = _submit_entry(tx_pool, status, entry.clone(), &self.callbacks)?; // (2) inserts new tx, can fail
...
tx_pool
    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
    .map_or(Ok(()), Err)?;  // (3) evicts entries if pool over limit, can fail
``` [1](#0-0) 

Step (1), `process_rbf`, unconditionally removes all conflicted transactions and places them in the conflicts cache (marking them as "rejected"):

```rust
let all_removed: Vec<_> = conflicts
    .iter()
    .flat_map(|id| tx_pool.pool_map.remove_entry_and_descendants(id))
    .collect();
...
tx_pool.record_conflict(old.transaction().clone());
``` [2](#0-1) 

This removal is **irreversible within the lock scope**. There is no rollback path. If step (2) or step (3) returns an error, the closure propagates `Err`, but the conflicted transactions remain permanently evicted.

The developers explicitly flag this in `pool_map.rs`:

```rust
// FIXME: In the scenario that a transaction passed all RBF rules, and then removed the conflicted
// transaction in txpool, then failed with max ancestor limits, we now need to rollback the removing.
// this is not an issue currently, because RBF have a rule that not allow any unknown inputs except
// the conflicted inputs, so the new transaction can not be in a long transaction chain.
// but it's still safer to report an error before any writing kind of operation.
``` [3](#0-2) 

The FIXME addresses only the `ExceededMaximumAncestorsCount` failure path in `_submit_entry`. It does **not** address the `limit_size` failure path in step (3). `limit_size` is called with the new entry's ID and can evict the newly inserted entry if it has a lower fee rate than other pool entries, returning an error that propagates out of the closure — after the conflicted transactions are already gone.

**Concrete attack path via `limit_size`:**

1. Attacker fills the pool with high-fee-rate transactions, bringing it near the size limit.
2. Victim has transaction `A` in the pool (small, high fee rate).
3. Attacker submits transaction `B` as an RBF replacement for `A`: `B` has a higher absolute fee (satisfying RBF Rule #3/#4) but is larger, giving it a lower fee rate than other pool entries.
4. `check_rbf` passes (absolute fee is sufficient).
5. `process_rbf` removes `A` from the pool and marks it as "RBFRejected" in the conflicts cache.
6. `_submit_entry` inserts `B` into the pool.
7. `limit_size` evicts `B` (lowest fee rate in the pool) and returns `Some(Reject::...)`.
8. The closure returns `Err`; `B` is also placed in the conflicts cache.
9. **Result:** `A` is permanently marked as rejected and absent from the pool; `B` is also absent. The victim's transaction is lost.

---

### Impact Explanation

A victim's pending transaction is permanently removed from the tx-pool and marked as "RBFRejected" without ever being committed to the chain or actually being superseded by a valid replacement. The victim must resubmit with a new transaction. If the attacker repeats this, the victim's transactions can be continuously evicted, preventing them from ever being committed — a griefing / denial-of-service against specific transaction senders. This maps to **permanent freezing of unclaimed yield** (the victim's transaction output remains unspendable until they successfully resubmit and get committed).

---

### Likelihood Explanation

- RBF must be enabled (`min_rbf_rate > min_fee_rate`), which is a configurable node option.
- The pool must be near its size limit (common under load).
- The attacker must craft a replacement transaction with higher absolute fee but lower fee rate — achievable by making the replacement transaction larger (e.g., more outputs or witnesses).
- The attacker pays a higher fee than the victim, so there is a cost, but the attack is repeatable and the cost is bounded.
- Entry point: the public `send_transaction` JSON-RPC method, reachable by any unprivileged RPC caller.

---

### Recommendation

Perform all validation and limit checks **before** any destructive write operations. Specifically, `check_and_record_ancestors` and `limit_size` feasibility checks should be run in a read-only or speculative mode before `process_rbf` removes conflicted transactions. If any post-removal step can fail, implement a rollback that re-inserts the removed transactions. The FIXME comment itself recommends this: *"it's still safer to report an error before any writing kind of operation."*

---

### Proof of Concept

1. Enable RBF on a CKB node (`min_rbf_rate > min_fee_rate`).
2. Fill the tx-pool to near its `max_tx_pool_size` with high-fee-rate transactions.
3. Submit victim transaction `A` (small, high fee rate) via `send_transaction`.
4. Submit attacker transaction `B` via `send_transaction`:
   - Same input as `A` (triggers RBF conflict detection).
   - Higher absolute fee than `A` (passes RBF fee rules).
   - Larger serialized size than `A` (lower fee rate, making it the eviction candidate for `limit_size`).
5. Observe: `A` is now in the conflicts cache with status `RBFRejected`; `B` is also in the conflicts cache (evicted by `limit_size`); neither is in the pending pool.
6. `A`'s inputs remain unspent on-chain, but `A` is gone from the pool. The victim must resubmit. [4](#0-3) [5](#0-4)

### Citations

**File:** tx-pool/src/process.rs (L96-170)
```rust
    pub(crate) async fn submit_entry(
        &self,
        pre_resolve_tip: Byte32,
        entry: TxEntry,
        mut status: TxStatus,
    ) -> (Result<(), Reject>, Arc<Snapshot>) {
        let (ret, snapshot) = self
            .with_tx_pool_write_lock(move |tx_pool, snapshot| {
                // check_rbf must be invoked in `write` lock to avoid concurrent issues.
                let conflicts = if tx_pool.enable_rbf() {
                    tx_pool.check_rbf(&snapshot, &entry)?
                } else {
                    // RBF is disabled but we found conflicts, return error here
                    // after_process will put this tx into conflicts_pool
                    let conflicted_outpoint =
                        tx_pool.pool_map.find_conflict_outpoint(entry.transaction());
                    if let Some(outpoint) = conflicted_outpoint {
                        return Err(Reject::Resolve(OutPointError::Dead(outpoint)));
                    }
                    HashSet::new()
                };

                // if snapshot changed by context switch we need redo time_relative verify
                let tip_hash = snapshot.tip_hash();
                if pre_resolve_tip != tip_hash {
                    debug!(
                        "submit_entry {} context changed. previous:{} now:{}",
                        entry.proposal_short_id(),
                        pre_resolve_tip,
                        tip_hash
                    );

                    // destructuring assignments are not currently supported
                    status = check_rtx(tx_pool, &snapshot, &entry.rtx)?;

                    let tip_header = snapshot.tip_header();
                    let tx_env = status.with_env(tip_header);
                    time_relative_verify(snapshot, Arc::clone(&entry.rtx), tx_env)?;
                }

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

                if !may_recovered_txs.is_empty() {
                    let self_clone = self.clone();
                    tokio::spawn(async move {
                        // push the recovered txs back to verify queue, so that they can be verified and submitted again
                        let mut queue = self_clone.verify_queue.write().await;
                        for tx in may_recovered_txs {
                            debug!("recover back: {:?}", tx.proposal_short_id());
                            let _ = queue.add_tx(tx, false, None);
                        }
                    });
                }
                Ok(())
            })
            .await;

        (ret, snapshot)
    }
```

**File:** tx-pool/src/process.rs (L203-231)
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
```

**File:** tx-pool/src/component/pool_map.rs (L582-640)
```rust
    /// Check ancestors and record for entry
    // FIXME: In the scenario that a transaction passed all RBF rules, and then removed the conflicted
    // transaction in txpool, then failed with max ancestor limits, we now need to rollback the removing.
    // this is not an issue currently, because RBF have a rule that not allow any unknown inputs except
    // the conflicted inputs, so the new transaction can not be in a long transaction chain.
    // but it's still safer to report an error before any writing kind of operation.
    fn check_and_record_ancestors(
        &mut self,
        entry: &mut TxEntry,
    ) -> Result<HashSet<TxEntry>, Reject> {
        let tx = entry.transaction();
        let (ancestors, mut parents, cell_ref_parents) = self.get_tx_ancenstors(tx);

        let mut ancestors_count = ancestors.len() + 1;
        let mut evicted = Default::default();

        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
        }

        if ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count {
            // if ancestors count exceed limitation,
            // try to evict some conflicted transactions due to ref cells

            // sort them to find out the transactions with lowest fees
            let evict_candidates: Vec<ProposalShortId> = self
                .entries
                .iter_by_evict_key()
                .filter(move |entry| cell_ref_parents.contains(&entry.id))
                .map(|x| x.id.clone())
                .collect();

            let mut iter = evict_candidates.iter();
            while ancestors_count > self.max_ancestors_count {
                if let Some(next_id) = iter.next() {
                    let removed = self.remove_entry_and_descendants(next_id);
                    ancestors_count = ancestors_count.saturating_sub(1);
                    parents.remove(next_id);
                    evicted.extend(removed);
                } else {
                    break;
                }
            }
        } else {
            return Err(Reject::ExceededMaximumAncestorsCount);
        }

        // some txs in `parents` are removed, now `ancestors` need to re-caculate,
        let ancestors = self
            .links
            .calc_relation_ids(parents.clone(), Relation::Parents);

        // we can assume the number now is less than `max_ancestors_count`
        assert!(ancestors.len() < self.max_ancestors_count);

        self._record_ancestors(entry, ancestors, parents);
        Ok(evicted)
    }
```
