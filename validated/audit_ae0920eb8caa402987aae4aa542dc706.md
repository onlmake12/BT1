Audit Report

## Title
Stale-Snapshot Overwrite in `add_entry` Inflates `total_tx_size`/`total_tx_cycles`, Enabling Tx-Pool DoS — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

`PoolMap::add_entry` captures a pre-eviction snapshot of `total_tx_size`/`total_tx_cycles` into local variables, then calls `check_and_record_ancestors`, which may evict entries and correctly decrement those fields via `update_stat_for_remove_tx`. After evictions complete, `add_entry` unconditionally overwrites `self.total_tx_size`/`self.total_tx_cycles` with the stale pre-eviction snapshot, permanently re-inflating the totals by the aggregate size and cycles of every evicted transaction. Repeated exploitation accumulates inflation until `limit_size` evicts all legitimate transactions and the pool rejects every new submission with `Reject::Full`.

## Finding Description

**Root cause — `add_entry`, lines 200–221:**

`updated_stat_for_add_tx` is a pure read (`&self`) that returns `(self.total_tx_size + tx_size, self.total_tx_cycles + cycles)` without modifying `self`. [1](#0-0)  The snapshot is stored in local variables at lines 210–211 before any mutations occur. [2](#0-1) 

`check_and_record_ancestors` is then called at line 213. When `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`, it evicts `cell_ref_parents` one by one via `remove_entry_and_descendants` at line 618. [3](#0-2) 

`remove_entry_and_descendants` calls `remove_entry` for each evicted entry. [4](#0-3)  `remove_entry` calls `update_stat_for_remove_tx` at line 247, which correctly decrements `self.total_tx_size` and `self.total_tx_cycles`. [5](#0-4) [6](#0-5) 

After all evictions and insertions complete, lines 218–219 unconditionally overwrite `self.total_tx_size` and `self.total_tx_cycles` with the stale pre-eviction snapshot: [7](#0-6) 

```
Step                                          self.total_tx_size
After updated_stat_for_add_tx snapshot        S + N  (stored in local var)
After evictions in check_and_record_ancestors S − E  (correct)
After overwrite at line 218                   S + N  (stale; E is lost)
```

Correct post-call value: `S − E + N`. Actual value: `S + N`. Inflation per call: `E`.

**Existing guards are insufficient:** `update_stat_for_remove_tx` has an underflow guard that falls back to `recompute_total_stat`, but this guard only fires on subtraction underflow. [8](#0-7)  The overwrite at lines 218–219 is an unconditional assignment that bypasses all guards entirely.

## Impact Explanation

`limit_size` in `pool.rs` loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size` (default 180 MB), evicting lowest-fee-rate transactions until the condition is false. [9](#0-8)  With `total_tx_size` artificially inflated, legitimate pending transactions are evicted even when actual memory usage is well within the limit, and new transactions are rejected with `Reject::Full`. Repeated exploitation accumulates inflation until `total_tx_size >= max_tx_pool_size`, at which point `limit_size` evicts all pending transactions and the pool permanently rejects every subsequent submission. The only recovery is a node restart.

This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (10001–15000 points)**.

## Likelihood Explanation

The trigger requires constructing a transaction graph where a new transaction has more than `max_ancestors_count` (default 25) ancestors and at least one ancestor is a `cell_ref_parent` (a transaction whose output is referenced as a cell dep). Referencing an unconfirmed transaction's output as a cell dep is a valid, protocol-permitted CKB pattern. No privileged access, no key material, and no majority hashpower is required. The attack is executable by any user with RPC access (`send_transaction`), including remote callers if the RPC port is exposed. Each attack round requires approximately 26 transactions; the inflation per round equals the size of the evicted dep-provider transaction(s). The attack is repeatable with fresh transactions each round.

## Recommendation

Move the stat update **after** all mutations (evictions, insertion) have completed, so it reflects the true post-eviction pool state. Replace the pre-eviction snapshot pattern with a call to `updated_stat_for_add_tx` placed after `check_and_record_ancestors` returns and after all insertions are done:

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
    // Overflow check only — do not store the snapshot
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    // Recompute AFTER evictions so evicted sizes are not re-added
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    self.total_tx_size = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;
    Ok((true, evicts))
}
```

Alternatively, remove the snapshot entirely and apply `self.total_tx_size += entry.size` / `self.total_tx_cycles += entry.cycles` (with overflow checks) after all mutations, mirroring the paired decrement in `update_stat_for_remove_tx`.

## Proof of Concept

**Setup (default config: `max_ancestors_count = 25`, `max_tx_pool_size = 180_000_000`):**

1. Submit a "dep-provider" transaction `D` to the pool. Record `size(D)`.
2. Submit a chain of 24 ancestor transactions `A1 → A2 → … → A24`.
3. Submit transaction `T` that spends `A24`'s output and references `D`'s output as a cell dep, making `D` a `cell_ref_parent` and pushing `ancestors_count` to 26 (> 25).

**Execution trace:**

1. `add_entry(T)` snapshots `total_tx_size = S + size(T)` into local var (line 210–211).
2. `check_and_record_ancestors`: `ancestors_count = 26 > 25`; `cell_ref_parents = {D}`; `26 − 1 = 25 ≤ 25` → eviction branch fires (line 603).
3. `remove_entry_and_descendants(D)` → `update_stat_for_remove_tx(size(D))` → `self.total_tx_size = S − size(D)` (line 247, 738–740).
4. Back in `add_entry`: `self.total_tx_size = total_tx_size` → `self.total_tx_size = S + size(T)` (line 218).
5. **Correct value:** `S − size(D) + size(T)`. **Actual value:** `S + size(T)`. **Inflation: `size(D)`.**

A unit test can be written directly against `PoolMap::add_entry` with `max_ancestors_count = 3` to reduce setup, asserting `pool_map.total_tx_size == expected_correct_value` after the call — this assertion will fail, confirming the bug.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-211)
```rust
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
```

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L247-247)
```rust
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
```

**File:** tx-pool/src/component/pool_map.rs (L252-264)
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
```

**File:** tx-pool/src/component/pool_map.rs (L603-625)
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
```

**File:** tx-pool/src/component/pool_map.rs (L710-729)
```rust
    /// Calculate size and cycles statistics for adding a tx.
    fn updated_stat_for_add_tx(
        &self,
        tx_size: usize,
        cycles: Cycle,
    ) -> Result<(usize, Cycle), Reject> {
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
    }
```

**File:** tx-pool/src/component/pool_map.rs (L733-741)
```rust
    fn update_stat_for_remove_tx(&mut self, tx_size: usize, cycles: Cycle) {
        match (
            self.total_tx_size.checked_sub(tx_size),
            self.total_tx_cycles.checked_sub(cycles),
        ) {
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
            }
```

**File:** tx-pool/src/component/pool_map.rs (L742-756)
```rust
            _ => {
                if let Some((total_tx_size, total_tx_cycles)) = self.recompute_total_stat() {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, recomputed size {} cycles {}",
                        tx_size, cycles, total_tx_size, total_tx_cycles
                    );
                    self.total_tx_size = total_tx_size;
                    self.total_tx_cycles = total_tx_cycles;
                } else {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, and recomputing overflowed",
                        tx_size, cycles
                    );
                }
            }
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
