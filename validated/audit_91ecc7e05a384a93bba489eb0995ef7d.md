Audit Report

## Title
Non-Atomic RBF Allows Permanent Mempool Eviction via Cell-Dep Ancestor Overflow Without Rollback — (`tx-pool/src/process.rs`, `tx-pool/src/component/pool_map.rs`)

## Summary
`submit_entry` in `process.rs` calls `process_rbf` to unconditionally remove conflicting transactions before calling `_submit_entry`. If `_submit_entry` subsequently fails with `ExceededMaximumAncestorsCount` — triggered by the new tx having ≥ `max_ancestors_count` ancestors via a cell_dep pointing to a deep in-pool chain — there is no rollback. The FIXME comment at `pool_map.rs:583–587` acknowledges this gap but incorrectly claims it is unreachable, because it only considers input-based ancestry while `get_tx_ancenstors` also counts cell_dep parents. An unprivileged attacker can exploit this to permanently evict targeted transactions from the mempool without inserting a replacement.

## Finding Description

**Non-atomic ordering in `submit_entry`:**

`process.rs:136–137` executes `process_rbf` before `_submit_entry`: [1](#0-0) 

`process_rbf` unconditionally removes all conflicting transactions and their descendants: [2](#0-1) 

**The FIXME's flawed reasoning:**

The comment at `pool_map.rs:582–587` claims the failure path is unreachable because "the new transaction can not be in a long transaction chain" due to the RBF rule blocking unknown inputs: [3](#0-2) 

This reasoning is incomplete. `get_tx_ancenstors` counts **cell_dep parents** as ancestors in addition to input parents: [4](#0-3) 

**The RBF cell_dep check is insufficient:**

`check_rbf` at `pool.rs:648–660` only rejects cell_deps pointing to the conflicting transactions themselves (`all_conflicted`). It does not block cell_deps pointing to other in-pool transactions: [5](#0-4) 

**The `calc_ancestors` disjointness check is a no-op for the new tx:**

`check_rbf` calls `calc_ancestors(&short_id)` for the new tx at `pool.rs:615`, but since the new tx is not yet in the pool, `links.inner` has no entry for it, so `calc_relative_ids` returns an empty set via `unwrap_or_default()`. The disjointness check at `pool.rs:626` therefore never catches cell_dep-based ancestry for the incoming tx: [6](#0-5) 

**The error path in `check_and_record_ancestors`:**

When `ancestors_count - cell_ref_parents.len() > max_ancestors_count`, the function returns `Err(Reject::ExceededMaximumAncestorsCount)` with no rollback of the already-removed conflicts: [7](#0-6) 

**Attack construction:**

1. Build a chain `tx_1 → tx_2 → ... → tx_26` in the pool (each spending the previous output). This gives `tx_26` exactly 25 in-pool ancestors.
2. Submit `conflict_tx` spending confirmed output `O`.
3. Submit RBF tx with: input = `O` (confirmed, satisfies Rule #2 "no unknown inputs"), cell_dep = output of `tx_26` (not a conflict, passes the cell_dep check), fee sufficient to pass RBF fee rules.
4. `check_rbf` passes all rules.
5. `process_rbf` removes `conflict_tx`.
6. `_submit_entry` → `add_entry` → `check_and_record_ancestors`: `get_tx_ancenstors` finds `tx_26` as a cell_dep parent; `ancestors` = {`tx_1`, …, `tx_26`}; `ancestors_count` = 27; `cell_ref_parents` = {} (no in-pool tx has a cell_dep on confirmed output `O`); `27 - 0 = 27 > 25` → `Err(ExceededMaximumAncestorsCount)`.
7. `conflict_tx` is permanently gone. The RBF tx is not inserted. No rollback occurs.

The attack is repeatable: the `tx_1`–`tx_26` chain remains in the pool and can be reused for subsequent evictions.

## Impact Explanation

An unprivileged attacker can permanently remove targeted transactions (and their descendants) from the mempool without inserting a replacement. This directly damages miner revenue by silently dropping high-fee transactions and can be used to disrupt CPFP packages or any targeted transaction chain. The attack is repeatable at low marginal cost after the initial chain setup. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, and potentially **Critical — Vulnerabilities which could easily damage CKB economy**, given that targeted, repeatable mempool eviction undermines transaction finality guarantees and miner incentives.

## Likelihood Explanation

No special privileges are required. The attack is executable via the standard `send_transaction` RPC or P2P relay. The setup (a chain of 25 transactions) is straightforward and inexpensive. The attack is deterministic and repeatable, with the `tx_1`–`tx_26` chain reusable across multiple eviction attempts. The only cost to the attacker is the fee for the RBF tx itself, which is never inserted.

## Recommendation

Move the ancestor-count pre-check **before** `process_rbf`, as the FIXME comment itself suggests. Specifically, in `submit_entry` (`process.rs`), call `get_tx_ancenstors` on the new tx and validate that `ancestors_count ≤ max_ancestors_count` before invoking `process_rbf`. This ensures the ancestor limit is enforced atomically before any pool state is mutated. Alternatively, implement a full rollback of removed transactions if `_submit_entry` fails, re-inserting them into the pool.

## Proof of Concept

1. Configure a node with default `max_ancestors_count = 25`.
2. Submit `tx_1 → tx_2 → ... → tx_26` (chain of 26 txs, each spending the previous output). Confirm all are in the pool.
3. Submit `conflict_tx` spending confirmed output `O`. Confirm it is in the pool.
4. Submit RBF tx: input = `O`, cell_dep = output of `tx_26`, fee > `min_replace_fee` for `conflict_tx`.
5. Assert: `conflict_tx` is absent from the pool; the RBF tx is absent from the pool; `tx_1`–`tx_26` remain in the pool.
6. Repeat steps 3–5 with a new `conflict_tx` to demonstrate repeatability at low cost.

### Citations

**File:** tx-pool/src/process.rs (L136-137)
```rust
                let may_recovered_txs = self.process_rbf(tx_pool, &entry, &conflicts);
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
