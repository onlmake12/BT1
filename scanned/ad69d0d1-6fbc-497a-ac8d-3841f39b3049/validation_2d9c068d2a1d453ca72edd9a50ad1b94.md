Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Overreported After Ancestor-Eviction in `add_entry` — (File: tx-pool/src/component/pool_map.rs)

## Summary

In `PoolMap::add_entry`, `updated_stat_for_add_tx` captures `self.total_tx_size + new_size` into a local variable before `check_and_record_ancestors` runs. When ancestor-eviction occurs, `remove_entry` correctly decrements `self.total_tx_size` via `update_stat_for_remove_tx`, but those decrements are immediately clobbered when the stale local variable is written back to `self.total_tx_size`. The result is a permanent per-eviction inflation of `total_tx_size` by the size of every evicted transaction, causing `limit_size()` to evict additional legitimate transactions from the pool.

## Finding Description

In `add_entry` (lines 210–219):

```
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // local = self.total_tx_size + new_size
evicts = self.check_and_record_ancestors(&mut entry)?;          // may call remove_entry → update_stat_for_remove_tx → self.total_tx_size -= S
...
self.total_tx_size = total_tx_size;   // OVERWRITES with stale pre-eviction value
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

`updated_stat_for_add_tx` reads `self.total_tx_size` and returns `self.total_tx_size + tx_size` without mutating the field: [2](#0-1) 

`check_and_record_ancestors` calls `remove_entry_and_descendants` when `ancestors_count > max_ancestors_count` and the excess is attributable to `cell_ref_parents`: [3](#0-2) 

`remove_entry` correctly calls `update_stat_for_remove_tx`, mutating `self.total_tx_size` in place: [4](#0-3) 

The state table:

| Step | `self.total_tx_size` | local `total_tx_size` |
|---|---|---|
| Before call | X | — |
| After `updated_stat_for_add_tx` | X (unchanged) | X + new_size |
| After eviction of S bytes | X − S | X + new_size (stale) |
| After assignment | **X + new_size (wrong)** | — |

Correct value is `X − S + new_size`. The evicted bytes S are never reflected in the committed total.

## Impact Explanation

`total_tx_size` is the sole guard in `limit_size()`: [5](#0-4) 

Each ancestor-eviction event inflates `total_tx_size` by S (the byte size of evicted transactions). When `limit_size()` subsequently runs, it sees a phantom excess and evicts S bytes worth of legitimate pending transactions to compensate. The attack is repeatable: each crafted submission that triggers the eviction path compounds the inflation and causes another round of legitimate-transaction eviction. The corrupted counters are also exposed via the `tx_pool_info` RPC. [6](#0-5) 

This matches the **High** impact: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs.* A sustained stream of crafted submissions can continuously drain the mempool of legitimate pending transactions, preventing them from being proposed or committed.

## Likelihood Explanation

The trigger requires `ancestors_count > max_ancestors_count` while `ancestors_count − cell_ref_parents.len() ≤ max_ancestors_count`. This is reachable by any unprivileged submitter: seed the pool with a chain of transactions sharing a cell dep, then submit a transaction whose ancestor count exceeds the limit by exactly the number of cell-dep parents. The default `max_ancestors_count` is 25, a depth easily reached on a live node. No key material, privileged access, or majority hashpower is required. The attack is cheap and repeatable.

## Recommendation

Move `updated_stat_for_add_tx` to after `check_and_record_ancestors` completes, so the addition is applied to the already-corrected `self.total_tx_size`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
// self.total_tx_size now reflects evictions; add the new tx on top
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
...
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, replace the local-variable pattern entirely with direct in-place mutation after all removals, or add an assertion/invariant test that `total_tx_size == recompute_total_stat().0` after every `add_entry` call. [7](#0-6) 

## Proof of Concept

1. Submit a chain of 24 transactions `T1 → T2 → … → T24` where each `Ti` uses the output of `T(i−1)` as a cell dep, making them `cell_ref_parents` of any future transaction referencing `T24`.
2. Submit `T25` that uses `T24`'s output as a cell dep and `T24`'s output as an input (or any input making T24 a direct parent). `ancestors_count = 25 = max_ancestors_count`; no eviction.
3. Submit `T26` that uses `T25`'s output as an input and `T24`'s output as a cell dep. `ancestors_count = 26 > 25`, `cell_ref_parents = {T24}`, `26 − 1 = 25 ≤ 25` → eviction path fires. `T24` (and its descendant `T25`) are removed; `update_stat_for_remove_tx` decrements `self.total_tx_size`. Then `self.total_tx_size = total_tx_size` (pre-eviction local) overwrites the corrected value.
4. Query `tx_pool_info` RPC: `total_tx_size` will exceed the sum of actual entry sizes by the size of the evicted transactions.
5. Observe that `limit_size()` subsequently evicts additional legitimate transactions to compensate for the phantom inflation.
6. Repeat steps 1–5 to accumulate inflation and continuously drain the mempool.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-219)
```rust
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
```

**File:** tx-pool/src/component/pool_map.rs (L247-248)
```rust
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

**File:** tx-pool/src/component/pool_map.rs (L698-708)
```rust
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

**File:** tx-pool/src/component/pool_map.rs (L711-720)
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
```

**File:** tx-pool/src/pool.rs (L298-307)
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
```

**File:** tx-pool/src/service.rs (L1089-1090)
```rust
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
```
