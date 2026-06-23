Let me examine the relevant code files to trace this potential vulnerability.

I have traced the full execution path through the relevant source files. Here is my analysis.

---

## Execution Flow Trace

**`submit_entry`** (process.rs:96–170) holds the write lock and executes this sequence:

1. `check_rbf` → validates rules, returns `conflict_ids` [1](#0-0) 
2. `process_rbf` → removes `conflict_tx` and all descendants from the pool [2](#0-1) 
3. `_submit_entry` → calls `add_pending`/`add_gap`/`add_proposed` → `pool_map.add_entry` → `check_and_record_ancestors` [3](#0-2) 

The FIXME comment explicitly acknowledges the missing rollback: [4](#0-3) 

---

## The FIXME Comment's Reasoning and Its Flaw

The comment claims the scenario is not reachable because:
> "RBF have a rule that not allow any unknown inputs except the conflicted inputs, so the new transaction can not be in a long transaction chain."

This reasoning is **incomplete**. It only considers input-based ancestry. However, `get_tx_ancenstors` also counts **cell_dep parents** as ancestors: [5](#0-4) 

The RBF cell_dep check in `check_rbf` only blocks cell_deps pointing to the **conflicting transactions themselves** — it does not block cell_deps pointing to other in-pool transactions: [6](#0-5) 

Additionally, `check_rbf` calls `calc_ancestors(&short_id)` for the new tx, but since the new tx is not yet in the pool, `links` has no entry for it, so this returns an empty set — the ancestor disjointness check is a no-op for the new tx: [7](#0-6) 

---

## Reachability of `ExceededMaximumAncestorsCount`

`check_and_record_ancestors` returns `Err(Reject::ExceededMaximumAncestorsCount)` when: [8](#0-7) 

```
ancestors_count > max_ancestors_count
AND
ancestors_count - cell_ref_parents.len() > max_ancestors_count
```

`cell_ref_parents` are in-pool transactions that have a cell_dep pointing to an output being **consumed as an input** by the new tx. After `process_rbf` removes `conflict_tx`, the new tx's inputs are all from confirmed outputs. If no in-pool transaction has a cell_dep on those confirmed outputs, `cell_ref_parents` is empty.

If the new tx has a cell_dep pointing to an in-pool transaction that itself has ≥ `max_ancestors_count` (default 25) ancestors, then `ancestors_count > 25` and `cell_ref_parents.len() = 0`, triggering the error path.

---

## Concrete Attack Scenario

1. Attacker builds a chain of 25+ transactions in the pool: `tx_1 → tx_2 → ... → tx_26` (each spending the previous output).
2. Attacker submits `conflict_tx` spending confirmed output `O`.
3. Attacker submits RBF tx with:
   - **Input**: `O` (same as `conflict_tx`, satisfying Rule #2)
   - **Cell_dep**: output of `tx_26` (which has 25 in-pool ancestors)
   - **Fee**: sufficient to pass RBF fee rules
4. `check_rbf` passes — cell_dep check only blocks deps on conflicting txs; `tx_26` is not a conflict. [6](#0-5) 
5. `process_rbf` removes `conflict_tx` and all its descendants. [9](#0-8) 
6. `_submit_entry` → `add_entry` → `check_and_record_ancestors`:
   - `get_tx_ancenstors` finds `tx_26` as a parent via cell_dep
   - `ancestors` = {tx_1, …, tx_26}, `ancestors_count = 27`
   - `cell_ref_parents` = {} (no in-pool tx has a cell_dep on `O`)
   - `27 - 0 = 27 > 25` → `Err(Reject::ExceededMaximumAncestorsCount)` [10](#0-9) 
7. `conflict_tx` and its descendants are permanently gone. The RBF tx is not inserted. No rollback occurs.

---

### Title
Non-Atomic RBF via Cell-Dep Ancestor Overflow Permanently Evicts Victim Transactions — (`tx-pool/src/component/pool_map.rs`, `tx-pool/src/process.rs`)

### Summary
An unprivileged attacker can permanently remove legitimate transactions from the mempool without inserting a replacement, by submitting an RBF transaction that passes all RBF checks but fails the ancestor-count limit via a cell_dep pointing to a deep in-pool chain.

### Finding Description
`submit_entry` (process.rs:136–137) calls `process_rbf` before `_submit_entry`. `process_rbf` unconditionally removes the conflicting transactions and their descendants. If `_submit_entry` subsequently fails — specifically if `check_and_record_ancestors` returns `ExceededMaximumAncestorsCount` because the new tx has ≥ `max_ancestors_count` ancestors via cell_deps — there is no rollback. The FIXME comment at pool_map.rs:583–587 acknowledges this but incorrectly claims it is unreachable because "the new transaction can not be in a long transaction chain." This reasoning ignores cell_dep-based ancestry, which is fully tracked by `get_tx_ancenstors` and counts toward the ancestor limit.

### Impact Explanation
The attacker permanently removes victim transactions (and their descendants) from the pool without replacing them. Miner revenue is damaged because high-fee transactions are silently dropped. The attack can be targeted at specific transaction chains (e.g., a CPFP package where a child pays for a parent).

### Likelihood Explanation
The attack requires no special privileges. It is executable via the standard `send_transaction` RPC or P2P relay. Setup requires submitting a chain of 25+ transactions (the default `max_ancestors_count`), which is straightforward. The attack is deterministic.

### Recommendation
Move the ancestor-count check **before** `process_rbf`, as the FIXME comment itself suggests ("it's still safer to report an error before any writing kind of operation"). Specifically, in `submit_entry`, pre-validate the new tx's ancestor count (including cell_dep parents) against `max_ancestors_count` before calling `process_rbf`. Alternatively, implement a rollback of the removed transactions if `_submit_entry` fails.

### Proof of Concept
1. Submit `tx_1 → tx_2 → ... → tx_26` (chain of 26 txs, each spending the previous output).
2. Submit `conflict_tx` spending confirmed output `O`.
3. Submit RBF tx: input = `O`, cell_dep = output of `tx_26`, fee > min_replace_fee.
4. Assert: `conflict_tx` is absent from pool, RBF tx is absent from pool, `tx_1`–`tx_26` remain.

### Citations

**File:** tx-pool/src/process.rs (L105-116)
```rust
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
```

**File:** tx-pool/src/process.rs (L136-136)
```rust
                let may_recovered_txs = self.process_rbf(tx_pool, &entry, &conflicts);
```

**File:** tx-pool/src/process.rs (L137-137)
```rust
                let evicted = _submit_entry(tx_pool, status, entry.clone(), &self.callbacks)?;
```

**File:** tx-pool/src/process.rs (L203-206)
```rust
        let all_removed: Vec<_> = conflicts
            .iter()
            .flat_map(|id| tx_pool.pool_map.remove_entry_and_descendants(id))
            .collect();
```

**File:** tx-pool/src/component/pool_map.rs (L541-547)
```rust
        for cell_dep in entry.cell_deps() {
            let dep_pt = cell_dep.out_point();
            let id = ProposalShortId::from_tx_hash(&dep_pt.tx_hash());
            if self.links.inner.contains_key(&id) {
                parents.insert(id);
            }
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

**File:** tx-pool/src/component/pool_map.rs (L603-628)
```rust
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
```

**File:** tx-pool/src/pool.rs (L615-630)
```rust
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
```

**File:** tx-pool/src/pool.rs (L648-660)
```rust
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
```
