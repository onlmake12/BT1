Audit Report

## Title
Stale-Snapshot Overwrite in `add_entry` Inflates `total_tx_size`/`total_tx_cycles`, Enabling Tx-Pool DoS — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

`PoolMap::add_entry` snapshots the projected pool totals before calling `check_and_record_ancestors`, which may evict existing entries and correctly decrement `self.total_tx_size`/`self.total_tx_cycles` via `update_stat_for_remove_tx`. Immediately after, `add_entry` unconditionally overwrites those fields with the pre-eviction snapshot, re-inflating the totals by the size and cycles of every evicted transaction. An unprivileged RPC caller can craft a transaction graph that repeatedly triggers this path, permanently inflating the pool's accounting until `limit_size` evicts all legitimate transactions and the pool rejects every new submission.

## Finding Description

**Root cause — `add_entry` lines 200–221:**

`updated_stat_for_add_tx` is called at line 210–211 and returns a local snapshot `(total_tx_size, total_tx_cycles) = (S + N, C + cycles_N)` where `S` is the current pool size and `N` is the new tx size. [1](#0-0) 

`check_and_record_ancestors` is then called at line 213. When `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`, the eviction branch at lines 603–625 fires, calling `remove_entry_and_descendants` for each `cell_ref_parent`. [2](#0-1) 

`remove_entry_and_descendants` calls `remove_entry` (line 263), which calls `update_stat_for_remove_tx` (line 247), correctly decrementing `self.total_tx_size` and `self.total_tx_cycles` by each evicted entry's size and cycles. [3](#0-2) 

After all evictions, `self.total_tx_size = S - E` (correct). But lines 218–219 then unconditionally overwrite with the stale pre-eviction snapshot: [4](#0-3) 

| Step | `self.total_tx_size` |
|---|---|
| After snapshot | `S + N` (local variable) |
| After evictions | `S - E` (correct) |
| After overwrite | `S + N` (stale — **inflation = E**) |

Correct post-call value should be `S - E + N`. Actual value is `S + N`. The pool is inflated by `E` (total size of evicted entries) per triggering call.

**Why existing checks fail:**

`updated_stat_for_add_tx` only performs an overflow check and returns a value — it does not mutate `self.total_tx_size` directly. [5](#0-4) 

`update_stat_for_remove_tx` correctly mutates `self.total_tx_size` during eviction, but the final overwrite at lines 218–219 discards that mutation entirely. [6](#0-5) 

There is no guard or reconciliation between the pre-eviction snapshot and the post-eviction state.

## Impact Explanation

`limit_size` in `pool.rs` evicts legitimate pending transactions whenever `self.pool_map.total_tx_size > self.config.max_tx_pool_size` (default 180 MB). [7](#0-6) 

With `total_tx_size` artificially inflated, `limit_size` evicts legitimate transactions even when actual memory usage is well within the limit. Repeated exploitation accumulates inflation until `total_tx_size >= max_tx_pool_size`, after which the pool permanently rejects all incoming transactions with `Reject::Full`. The only recovery is a node restart. This matches the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs* (10001–15000 points). A node whose mempool permanently rejects all transactions cannot propagate user transactions to miners, causing effective network congestion for all users of that node.

## Likelihood Explanation

The trigger requires: (1) submitting a chain of 25 ancestor transactions (valid, standard CKB usage), (2) submitting a "dep-provider" transaction whose output is referenced as a cell dep, and (3) submitting a new transaction that references the dep-provider as a cell dep and has 26 ancestors total. This is a protocol-permitted pattern in CKB. No privileged access, no key material, and no majority hashpower is required. The attack is executable by any user with RPC access (including remote callers if the RPC port is exposed). Each round inflates the pool by `size(D)` (a few hundred bytes minimum), so reaching 180 MB requires many rounds but is fully automatable. The `max_ancestors_count` default of 25 is low enough that the required chain depth is trivially achievable. [8](#0-7) 

## Recommendation

Move the stat update **after** all evictions have completed. The simplest correct fix is to not store the snapshot at all and instead apply the increment directly after `check_and_record_ancestors` returns:

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
    // Validate capacity (overflow check) without storing the snapshot
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    // Apply increment AFTER evictions so evicted sizes are not re-added
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    self.total_tx_size = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;
    Ok((true, evicts))
}
```

Alternatively, replace the snapshot pattern entirely with a direct `self.total_tx_size += entry.size` after all mutations, paired with the existing `update_stat_for_remove_tx` decrements.

## Proof of Concept

**Setup (default config: `max_ancestors_count = 25`, `max_tx_pool_size = 180_000_000`):**

1. Submit 24 ancestor transactions `A1 → A2 → … → A24` (each spending the previous output).
2. Submit a dep-provider transaction `D` whose output `D:0` is a live cell dep.
3. Submit a new transaction `T` that spends `A24`'s output **and** references `D:0` as a cell dep, making `D` a `cell_ref_parent` of `T`.

**Execution trace:**

1. `add_entry(T)` called; `updated_stat_for_add_tx` snapshots `total_tx_size = S + size(T)`.
2. `check_and_record_ancestors`: `ancestors_count = 26 > 25`; `cell_ref_parents = {D}`; `26 - 1 = 25 <= 25` → eviction branch fires.
3. `remove_entry_and_descendants(D)` → `update_stat_for_remove_tx(size(D), ...)` → `self.total_tx_size = S - size(D)`.
4. Back in `add_entry`: `self.total_tx_size = total_tx_size` → `self.total_tx_size = S + size(T)`.
5. Correct value: `S - size(D) + size(T)`. Actual: `S + size(T)`. **Inflation per round: `size(D)`.**

Repeat with fresh `D` transactions each round. After enough rounds, `total_tx_size >= 180_000_000`, `limit_size` evicts all pending transactions, and the pool rejects every subsequent submission with `Reject::Full`.

A unit test can be written against `PoolMap` directly: assert that after one round of the above, `pool_map.total_tx_size` equals the sum of sizes of all entries actually present in the pool (verifiable via `recompute_total_stat`). [9](#0-8)

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

**File:** tx-pool/src/component/pool_map.rs (L693-708)
```rust
                .proposed
                .set(self.proposed_count as i64);
        }
    }

    fn recompute_total_stat(&self) -> Option<(usize, Cycle)> {
        self.entries.iter().try_fold(
            (0usize, 0 as Cycle),
            |(total_size, total_cycles), (_, entry)| {
                Some((
                    total_size.checked_add(entry.inner.size)?,
                    total_cycles.checked_add(entry.inner.cycles)?,
                ))
            },
        )
    }
```

**File:** tx-pool/src/component/pool_map.rs (L711-729)
```rust
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

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```

**File:** resource/ckb.toml (L211-216)
```text
max_tx_pool_size = 180_000_000 # 180mb
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
max_tx_verify_cycles = 70_000_000
max_ancestors_count = 25
```
