The actual code confirms the claim. Let me verify the key details:

**Lines 210–211**: `updated_stat_for_add_tx` computes `(S + N, C + cycles_N)` into local variables — it does **not** mutate `self.total_tx_size`. [1](#0-0) 

**Lines 213**: `check_and_record_ancestors` may call `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx` at line 247, which **does** mutate `self.total_tx_size`. [2](#0-1) 

**Lines 218–219**: After evictions have correctly decremented `self.total_tx_size`, these lines unconditionally overwrite it with the stale pre-eviction snapshot. [3](#0-2) 

The eviction branch at lines 603–625 is reachable when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. [4](#0-3) 

`limit_size` in `pool.rs` evicts based on the inflated `total_tx_size`. [5](#0-4) 

The bug is real and the exploit path is reachable by an unprivileged user.

---

Audit Report

## Title
Stale-Snapshot Overwrite in `add_entry` Inflates `total_tx_size`/`total_tx_cycles`, Enabling Tx-Pool DoS — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

`PoolMap::add_entry` snapshots projected pool totals before calling `check_and_record_ancestors`, which may evict existing entries and correctly decrement `self.total_tx_size`/`self.total_tx_cycles` via `update_stat_for_remove_tx`. Immediately after, `add_entry` unconditionally overwrites those fields with the pre-eviction snapshot, re-inflating the totals by the size and cycles of every evicted transaction. An unprivileged caller can craft a transaction graph that repeatedly triggers this path, permanently inflating the pool's accounting until `limit_size` evicts all legitimate transactions and the pool rejects every new submission with `Reject::Full`.

## Finding Description

**Root cause — `add_entry` lines 200–221:**

At lines 210–211, `updated_stat_for_add_tx(entry.size, entry.cycles)` computes `(S + N, C + cycles_N)` into local variables `total_tx_size` and `total_tx_cycles`. This function is a pure computation — it does not mutate `self.total_tx_size`.

At line 213, `check_and_record_ancestors` is called. When `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`, the eviction branch at lines 603–625 fires. It calls `remove_entry_and_descendants` for each `cell_ref_parent`, which calls `remove_entry` (line 263), which calls `update_stat_for_remove_tx` (line 247). This **correctly mutates** `self.total_tx_size` and `self.total_tx_cycles` downward by each evicted entry's size and cycles.

After all evictions complete, `self.total_tx_size = S - E` (correct). But lines 218–219 then unconditionally overwrite with the stale pre-eviction snapshot:

| Step | `self.total_tx_size` |
|---|---|
| Before `add_entry` | `S` |
| Snapshot computed | `S + N` (local variable) |
| After evictions via `update_stat_for_remove_tx` | `S - E` (correct) |
| After overwrite at lines 218–219 | `S + N` (stale — **inflation = E**) |

Correct post-call value: `S - E + N`. Actual value: `S + N`. Inflation per triggering call: `E` (total size of evicted entries).

**Why existing checks fail:**

`updated_stat_for_add_tx` only performs an overflow check and returns a value — it does not mutate `self.total_tx_size` directly. `update_stat_for_remove_tx` correctly mutates `self.total_tx_size` during eviction, but the final overwrite at lines 218–219 discards that mutation entirely. There is no guard or reconciliation between the pre-eviction snapshot and the post-eviction state.

## Impact Explanation

`limit_size` in `pool.rs` evicts legitimate pending transactions whenever `self.pool_map.total_tx_size > self.config.max_tx_pool_size` (default 180 MB). With `total_tx_size` artificially inflated, `limit_size` evicts legitimate transactions even when actual memory usage is well within the limit. Repeated exploitation accumulates inflation until `total_tx_size >= max_tx_pool_size`, after which the pool permanently rejects all incoming transactions with `Reject::Full`. The only recovery is a node restart. This matches the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs* (10001–15000 points). A node whose mempool permanently rejects all transactions cannot propagate user transactions to miners, causing effective network congestion for all users of that node.

## Likelihood Explanation

The trigger requires: (1) submitting a chain of ancestor transactions up to `max_ancestors_count - 1` depth (valid, standard CKB usage), (2) submitting a dep-provider transaction `D` whose output is used as a cell dep by another in-pool transaction that also references the chain tip's output, and (3) submitting a new transaction `T` that spends the chain tip's output, making `D` a `cell_ref_parent` of `T` via the `edges.deps` lookup. No privileged access, no key material, and no majority hashpower is required. The attack is executable by any user with RPC access. Each round inflates the pool by `size(D)` (a few hundred bytes minimum), so reaching 180 MB requires many rounds but is fully automatable. The `max_ancestors_count` default of 25 is low enough that the required chain depth is trivially achievable.

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
2. Submit a dep-provider transaction `D` that uses `A24`'s output as a cell dep (so `edges.deps[A24:0]` contains `D`).
3. Submit a new transaction `T` that spends `A24`'s output as an input. When `get_tx_ancenstors` processes `T`'s input `A24:0`, it finds `D` in `edges.deps[A24:0]`, adding `D` to `cell_ref_parents`. `ancestors_count = 26 > 25`; `26 - 1 = 25 <= 25` → eviction branch fires.

**Execution trace:**

1. `add_entry(T)` called; `updated_stat_for_add_tx` snapshots `total_tx_size = S + size(T)` into local variable.
2. `check_and_record_ancestors`: eviction branch fires, `remove_entry_and_descendants(D)` → `update_stat_for_remove_tx(size(D), ...)` → `self.total_tx_size = S - size(D)`.
3. Back in `add_entry`: `self.total_tx_size = total_tx_size` → `self.total_tx_size = S + size(T)`.
4. Correct value: `S - size(D) + size(T)`. Actual: `S + size(T)`. **Inflation per round: `size(D)`.**

Repeat with fresh `D` transactions each round. After enough rounds, `total_tx_size >= 180_000_000`, `limit_size` evicts all pending transactions, and the pool rejects every subsequent submission with `Reject::Full`.

A unit test can be written against `PoolMap` directly: assert that after one round of the above, `pool_map.total_tx_size` equals the sum of sizes of all entries actually present in the pool (verifiable via `recompute_total_stat`).

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

**File:** tx-pool/src/component/pool_map.rs (L246-248)
```rust
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
            entry.inner
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

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```
