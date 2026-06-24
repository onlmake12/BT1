Audit Report

## Title
`PoolMap::add_entry` Overwrites Post-Eviction Accounting State with Stale Pre-Computed Totals, Inflating `total_tx_size`/`total_tx_cycles` — (File: tx-pool/src/component/pool_map.rs)

## Summary

In `PoolMap::add_entry`, local snapshots of `total_tx_size` and `total_tx_cycles` are captured before a conditional eviction step (`check_and_record_ancestors`), then unconditionally written back to `self` after that step. When evictions occur, `remove_entry` correctly decrements the counters via `update_stat_for_remove_tx`, but those correct values are immediately overwritten by the stale pre-eviction snapshots. This permanently inflates `total_tx_size` and `total_tx_cycles` by the size/cycles of every evicted transaction, compounding on each trigger.

## Finding Description

The exact sequence in `add_entry` (lines 200–221): [1](#0-0) 

1. **Line 210–211**: `updated_stat_for_add_tx` reads `self.total_tx_size` and `self.total_tx_cycles`, adds `entry.size`/`entry.cycles`, and stores the result in **local variables** `total_tx_size` and `total_tx_cycles`.

2. **Line 213**: `check_and_record_ancestors` may call `remove_entry_and_descendants` → `remove_entry`, which calls `update_stat_for_remove_tx` at line 247, **directly mutating** `self.total_tx_size` and `self.total_tx_cycles` to the correct post-eviction values: [2](#0-1) [3](#0-2) 

3. **Lines 218–219**: The stale local variables (computed before any eviction) are unconditionally written back, overwriting the correct post-eviction `self.total_tx_size` and `self.total_tx_cycles`.

The eviction path in `check_and_record_ancestors` fires when `ancestors_count > max_ancestors_count` but the excess is entirely attributable to `cell_ref_parents`: [4](#0-3) 

**Accounting drift per trigger:**

| Step | `self.total_tx_size` |
|---|---|
| Before `add_entry` | `X` |
| After `updated_stat_for_add_tx` (local var) | `X + entry.size` |
| After evictions via `update_stat_for_remove_tx` | `X − evicted_size` |
| After line 218 overwrites | `X + entry.size` ← evicted_size never subtracted |
| Correct value | `X − evicted_size + entry.size` |

Each trigger inflates by `evicted_size` (and `evicted_cycles`). Repeated triggering compounds the drift monotonically.

## Impact Explanation

`total_tx_size` is the sole gate for two critical pool decisions:

1. **`limit_size` eviction loop** — evicts legitimate transactions until `total_tx_size <= max_tx_pool_size`. An inflated counter causes the loop to evict fee-paying transactions that should remain in the pool: [5](#0-4) 

2. **`updated_stat_for_add_tx` admission check** — rejects new transactions with `Reject::Full` when the inflated counter makes the pool appear full: [6](#0-5) 

Repeated triggering causes the pool to progressively reject all new transactions and evict existing ones, constituting a sustained denial-of-service against the mempool. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10001–15000 points)**.

## Likelihood Explanation

The eviction path is reachable by any unprivileged user via the standard `send_transaction` RPC or P2P relay. The attacker only needs to:
- Control a spendable output cell `X` on-chain (one transaction).
- Submit ≥ 126 independent transactions each referencing `X` as a `cell_dep` (cheap, no special keys required).
- Submit one transaction spending `X` as an input to trigger the eviction path.

This is repeatable: after each trigger, the attacker can re-submit new `cell_dep` transactions and repeat. The cost per trigger is only the transaction fees for ~126 small transactions. The inflation compounds with each repetition until the pool is effectively disabled.

## Recommendation

Move `updated_stat_for_add_tx` to **after** `check_and_record_ancestors` completes, so it reads the already-correct post-eviction `self.total_tx_size`/`self.total_tx_cycles`:

```diff
 pub(crate) fn add_entry(
     &mut self,
     mut entry: TxEntry,
     status: Status,
 ) -> Result<(bool, HashSet<TxEntry>), Reject> {
     let tx_short_id = entry.proposal_short_id();
     let mut evicts = Default::default();
     if self.entries.get_by_id(&tx_short_id).is_some() {
         return Ok((false, evicts));
     }
-    let (total_tx_size, total_tx_cycles) =
-        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
     trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
     evicts = self.check_and_record_ancestors(&mut entry)?;
     self.record_entry_edges(&entry)?;
     self.insert_entry(&entry, status);
     self.record_entry_descendants(&entry);
     self.track_entry_statics(None, Some(status));
+    let (total_tx_size, total_tx_cycles) =
+        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
     self.total_tx_size = total_tx_size;
     self.total_tx_cycles = total_tx_cycles;
     Ok((true, evicts))
 }
```

Note: the admission check in `updated_stat_for_add_tx` remains correct after the move — it now reads the post-eviction `self.total_tx_size`, which is the semantically correct value to check against pool capacity.

## Proof of Concept

1. Deploy a script cell `X` on-chain (attacker-controlled output).
2. Submit 126 independent transactions `T1…T126` to the node's tx-pool, each using cell `X` as a `cell_dep`. All are accepted; `total_tx_size = sum(sizes(T1..T126))`.
3. Submit transaction `T_new` that **spends** cell `X` as an input (consuming it). `T_new` has no other pool ancestors.
4. Inside `add_entry` for `T_new`:
   - `updated_stat_for_add_tx` snapshots `total_tx_size_pre = sum(T1..T126) + size(T_new)` into a local variable.
   - `check_and_record_ancestors` finds 126 `cell_ref_parents` (T1…T126), exceeding `max_ancestors_count=125`. It evicts the lowest-fee entry (e.g., `T126`) via `remove_entry_and_descendants` → `update_stat_for_remove_tx`, setting `self.total_tx_size -= size(T126)`.
   - Line 218 writes `self.total_tx_size = total_tx_size_pre`, restoring the pre-eviction value and losing the subtraction of `size(T126)`.
5. `total_tx_size` is now inflated by `size(T126)`.
6. Repeat steps 2–4 to compound the inflation. After enough repetitions, `limit_size` begins evicting legitimate transactions or `updated_stat_for_add_tx` rejects all new submissions with `Reject::Full`.

A unit test can verify this by: constructing a `PoolMap` with `max_ancestors_count=2`, inserting 3 `cell_dep` transactions referencing a shared cell, then inserting a spending transaction, and asserting that `pool_map.total_tx_size` equals the sum of sizes of all remaining entries (verifiable via `recompute_total_stat`). [7](#0-6) [8](#0-7)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L200-221)
```rust
    pub(crate) fn add_entry(
        &mut self,
        mut entry: TxEntry,
        status: Status,
    ) -> Result<(bool, HashSet<TxEntry>), Reject> {
        let tx_short_id = entry.proposal_short_id();
        let mut evicts = Default::default();
        if self.entries.get_by_id(&tx_short_id).is_some() {
            return Ok((false, evicts));
        }
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
        trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
        evicts = self.check_and_record_ancestors(&mut entry)?;
        self.record_entry_edges(&entry)?;
        self.insert_entry(&entry, status);
        self.record_entry_descendants(&entry);
        self.track_entry_statics(None, Some(status));
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
        Ok((true, evicts))
    }
```

**File:** tx-pool/src/component/pool_map.rs (L247-247)
```rust
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
```

**File:** tx-pool/src/component/pool_map.rs (L588-640)
```rust
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

**File:** tx-pool/src/component/pool_map.rs (L716-728)
```rust
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_size {} overflows by add {}",
                self.total_tx_size, tx_size
            ))
        })?;
        let total_tx_cycles = self.total_tx_cycles.checked_add(cycles).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_cycles {} overflows by add {}",
                self.total_tx_cycles, cycles
            ))
        })?;
        Ok((total_tx_size, total_tx_cycles))
```

**File:** tx-pool/src/component/pool_map.rs (L738-740)
```rust
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/pool.rs (L298-326)
```rust
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
```
