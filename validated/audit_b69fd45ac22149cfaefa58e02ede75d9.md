### Title
Missing Rollback of Evicted Transactions After Failed RBF Insertion — (`tx-pool/src/component/pool_map.rs`, `tx-pool/src/process.rs`)

---

### Summary

In the RBF (Replace-By-Fee) submission flow, conflicted transactions are permanently removed from the tx-pool **before** the replacement transaction is successfully inserted. If the insertion step fails for any reason, there is no rollback path to restore the evicted transactions. The codebase itself contains a `FIXME` comment acknowledging this gap. This is a direct structural analog to the external report's vulnerability class: a multi-step operation where an early destructive step (removal/burn) is not rolled back when a later step fails.

---

### Finding Description

In `tx-pool/src/process.rs`, the `submit_entry` function executes the RBF flow in two sequential, non-atomic steps:

1. **`process_rbf`** — permanently removes all conflicted transactions (and their descendants) from the pool and records them in the conflicts cache.
2. **`_submit_entry`** — attempts to insert the new replacement transaction into the pool.

```rust
// tx-pool/src/process.rs ~line 136-152
let may_recovered_txs = self.process_rbf(tx_pool, &entry, &conflicts);
let evicted = _submit_entry(tx_pool, status, entry.clone(), &self.callbacks)?;
// ...
tx_pool
    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
    .map_or(Ok(()), Err)?;
```

The `?` operator on `_submit_entry` means any error propagates immediately, returning from `submit_entry` without restoring the transactions removed by `process_rbf`. Similarly, the subsequent `limit_size` call can return an error after the new tx has been inserted and the old txs are already gone.

The developers explicitly acknowledge this gap in `tx-pool/src/component/pool_map.rs`:

```
// FIXME: In the scenario that a transaction passed all RBF rules, and then removed the conflicted
// transaction in txpool, then failed with max ancestor limits, we now need to rollback the removing.
// this is not an issue currently, because RBF have a rule that not allow any unknown inputs except
```

The comment is **truncated**, leaving the stated mitigation rationale incomplete and unverifiable from the source alone. No actual rollback code exists anywhere in the RBF path.

Inside `process_rbf` (lines 203–231), the removal is unconditional and immediate:

```rust
let all_removed: Vec<_> = conflicts
    .iter()
    .flat_map(|id| tx_pool.pool_map.remove_entry_and_descendants(id))
    .collect();
// ...
for old in all_removed {
    tx_pool.record_conflict(old.transaction().clone());
    self.callbacks.call_reject(tx_pool, &old, reject);
}
```

Once `record_conflict` and `call_reject` are invoked, the evicted transactions are treated as permanently rejected. There is no mechanism to re-insert them into the pending pool if the replacement subsequently fails.

---

### Impact Explanation

If the replacement transaction fails after `process_rbf` completes:

- The conflicted transactions are permanently gone from the pending pool.
- The replacement transaction is not in the pool either.
- The affected inputs are now "free" (not referenced by any pool transaction), but the original submitters must manually detect the eviction and resubmit.
- For transactions with time-sensitive `since` fields (e.g., relative or absolute time locks), missing the resubmission window results in the transaction becoming permanently invalid, effectively causing loss of the locked capacity or opportunity.
- An attacker who can craft a transaction that passes all RBF checks but causes `_submit_entry` or `limit_size` to fail can use this to silently evict victim transactions from the pool with no on-chain trace and no error returned to the victim.

---

### Likelihood Explanation

The FIXME comment states the issue is "not an issue currently" due to an RBF rule, but the comment is cut off and the reasoning is incomplete. RBF Rule #2 (`check_rbf`, `pool.rs` lines 596–609) requires that the new tx's inputs are either inputs of the conflicted txs or confirmed on-chain, which limits the ancestor count of the replacement to zero after eviction. This makes `_submit_entry` failure via ancestor-count limits unlikely under current rules.

However:
- The `limit_size` call after `_submit_entry` can still return an error if the pool is at capacity and the replacement has a lower fee rate than other pool entries, leaving the pool in a state where neither the old nor the new transactions are present.
- The FIXME is self-acknowledged and the truncated comment means the full mitigation argument is not auditable.
- Any future change to RBF rules (e.g., relaxing Rule #2) could immediately make this exploitable without any new code being introduced.

---

### Recommendation

1. **Atomic replacement**: Collect all transactions to be removed, attempt `_submit_entry` first (or validate that it will succeed), and only then commit the removal. This mirrors the "bundle all checks before destructive action" pattern recommended in the external report.
2. **Explicit rollback**: If `_submit_entry` or `limit_size` fails after `process_rbf` has run, re-insert the removed transactions back into the pending pool before returning the error.
3. **Complete the FIXME**: The truncated comment at `pool_map.rs:582–585` should be completed with a full explanation of why the current rules prevent exploitation, or the rollback should be implemented.

---

### Proof of Concept

A tx-pool submitter triggers the gap as follows:

1. Submit `tx_A` spending `outpoint_X` (now in the pending pool).
2. Craft `tx_B` spending `outpoint_X` with a fee high enough to pass all RBF checks (`check_rbf` returns `Ok`).
3. Arrange for the pool to be at its size limit with higher-fee-rate transactions such that `limit_size` will evict `tx_B` immediately after insertion.
4. Submit `tx_B` via the RPC `send_transaction`.
5. `process_rbf` removes `tx_A` and calls `call_reject` on it (permanent eviction).
6. `_submit_entry` succeeds (adds `tx_B`).
7. `limit_size` evicts `tx_B` (lowest fee rate in the now-full pool) and returns `Err`.
8. `submit_entry` propagates the error via `?`.
9. Result: `tx_A` is gone, `tx_B` is gone, `outpoint_X` is free — the original submitter of `tx_A` receives no notification and must detect the eviction by polling.

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** tx-pool/src/process.rs (L190-234)
```rust
    fn process_rbf(
        &self,
        tx_pool: &mut TxPool,
        entry: &TxEntry,
        conflicts: &HashSet<ProposalShortId>,
    ) -> Vec<TransactionView> {
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

**File:** tx-pool/src/component/pool_map.rs (L582-585)
```rust
    /// Check ancestors and record for entry
    // FIXME: In the scenario that a transaction passed all RBF rules, and then removed the conflicted
    // transaction in txpool, then failed with max ancestor limits, we now need to rollback the removing.
    // this is not an issue currently, because RBF have a rule that not allow any unknown inputs except
```

**File:** tx-pool/src/pool.rs (L574-676)
```rust
    pub(crate) fn check_rbf(
        &self,
        snapshot: &Snapshot,
        entry: &TxEntry,
    ) -> Result<HashSet<ProposalShortId>, Reject> {
        assert!(self.enable_rbf());
        let tx_inputs: Vec<OutPoint> = entry.transaction().input_pts_iter().collect();
        let conflict_ids = self.pool_map.find_conflict_tx(entry.transaction());

        if conflict_ids.is_empty() {
            return Ok(HashSet::new());
        }

        let short_id = entry.proposal_short_id();

        // Rule #1, the node has enabled RBF, which is checked by caller
        let conflicts = conflict_ids
            .iter()
            .filter_map(|id| self.get_pool_entry(id))
            .collect::<Vec<_>>();
        assert!(conflicts.len() == conflict_ids.len());

        // Rule #2, new tx don't contain any new unconfirmed inputs
        let mut inputs = HashSet::new();
        for c in conflicts.iter() {
            inputs.extend(c.inner.transaction().input_pts_iter());
        }

        if tx_inputs
            .iter()
            .any(|pt| !inputs.contains(pt) && !snapshot.transaction_exists(&pt.tx_hash()))
        {
            return Err(Reject::RBFRejected(
                "new Tx contains unconfirmed inputs".to_string(),
            ));
        }

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

            if !descendants.is_disjoint(&ancestors) {
                return Err(Reject::RBFRejected(
                    "Tx ancestors have common with conflict Tx descendants".to_string(),
                ));
            }

            let entries = descendants
                .iter()
                .filter_map(|id| self.get_pool_entry(id))
                .collect::<Vec<_>>();

            for entry in entries.iter() {
                let hash = entry.inner.transaction().hash();
                if tx_inputs.iter().any(|pt| pt.tx_hash() == hash) {
                    return Err(Reject::RBFRejected(
                        "new Tx contains inputs in descendants of to be replaced Tx".to_string(),
                    ));
                }
            }
            all_conflicted.extend(entries);
        }

        let tx_cells_deps: Vec<OutPoint> = entry
            .transaction()
            .cell_deps_iter()
            .map(|c| c.out_point())
            .collect();
        for entry in all_conflicted.iter() {
            let hash = entry.inner.transaction().hash();
            if tx_cells_deps.iter().any(|pt| pt.tx_hash() == hash) {
                return Err(Reject::RBFRejected(
                    "new Tx contains cell deps from conflicts".to_string(),
                ));
            }
        }

        // Rule #4, new tx's fee need to higher than min_rbf_fee computed from the tx_pool configuration
        // Rule #3, new tx's fee need to higher than conflicts, here we only check the all conflicted txs fee
        let fee = entry.fee;
        if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
            if fee < min_replace_fee {
                return Err(Reject::RBFRejected(format!(
                    "Tx's current fee is {}, expect it to >= {} to replace old txs",
                    fee, min_replace_fee,
                )));
            }
        } else {
            return Err(Reject::RBFRejected(
                "calculate_min_replace_fee failed".to_string(),
            ));
        }
```
