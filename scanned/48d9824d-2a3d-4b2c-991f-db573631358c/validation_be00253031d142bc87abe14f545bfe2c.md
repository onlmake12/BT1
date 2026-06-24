The code confirms the claim. Let me verify the complete call chain before rendering a verdict.

The confirmed execution path in `add_entry` (lines 200–221):
- Line 210–211: `updated_stat_for_add_tx` is `&self` (immutable borrow), returns a **local** `(total_tx_size, total_tx_cycles)` snapshot. [1](#0-0) 
- Line 213: `check_and_record_ancestors` is called, which at line 618 calls `remove_entry_and_descendants`. [2](#0-1) 
- `remove_entry_and_descendants` (line 263) calls `self.remove_entry(id)` for each removed entry. [3](#0-2) 
- `remove_entry` (line 247) calls `update_stat_for_remove_tx`, which **mutates `self.total_tx_size` in-place**. [4](#0-3) 
- Lines 218–219: the stale local values are unconditionally written back, overwriting the correctly decremented `self.total_tx_size`. [5](#0-4) 

The eviction path is gated by `ancestors_count > max_ancestors_count` AND `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (lines 598–628), which is reachable by any submitter who builds a chain exceeding the ancestor limit with cell-dep-referencing entries. [6](#0-5) 

---

Audit Report

## Title
`total_tx_size` permanently inflated after cell_ref_parent eviction in `add_entry` — (`tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::add_entry` pre-computes the new pool size totals into local variables before calling `check_and_record_ancestors`, which may evict existing transactions via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, each of which mutates `self.total_tx_size` in-place. The stale pre-eviction local value is then unconditionally written back to `self.total_tx_size` at line 218, permanently inflating it by the sum of all evicted transaction sizes. The invariant `total_tx_size == Σ entries[i].size` is broken after every such eviction and the error accumulates across repeated submissions.

## Finding Description
In `PoolMap::add_entry` (lines 200–221):

1. `updated_stat_for_add_tx` (lines 210–211) is a `&self` (immutable) method that snapshots `self.total_tx_size + entry.size` into a local variable `total_tx_size`. It does not modify `self`.
2. `check_and_record_ancestors` (line 213) enters the eviction branch when `ancestors_count > max_ancestors_count` AND `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (lines 603–625). It calls `self.remove_entry_and_descendants(next_id)` in a loop.
3. `remove_entry_and_descendants` (lines 252–265) calls `self.remove_entry(id)` for each removed entry (line 263).
4. `remove_entry` (lines 235–249) calls `self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles)` (line 247), which decrements `self.total_tx_size` in-place (lines 738–740).
5. After `check_and_record_ancestors` returns, line 218 executes `self.total_tx_size = total_tx_size`, overwriting the correctly decremented value with the stale snapshot from step 1.

The net result: if N transactions of total size `evicted_sum` are evicted, `self.total_tx_size` ends up as `T + new_size` instead of the correct `T - evicted_sum + new_size`. The inflation of `evicted_sum` is permanent. The only recovery path is `recompute_total_stat`, which is only triggered on underflow in `update_stat_for_remove_tx` (lines 742–755) — a separate, unrelated condition.

## Impact Explanation
`total_tx_size` is the primary signal used to determine whether the pool is full and to drive eviction of low-fee transactions. An inflated counter causes the pool to believe it is fuller than it actually is, triggering premature eviction of legitimate high-fee transactions and incorrect rejection of new submissions. An attacker can repeat the trigger to accumulate inflation across multiple submissions, progressively degrading mempool capacity. This constitutes a **High** impact: a vulnerability or bad design that can cause CKB network congestion with relatively low cost, matching the allowed impact class "Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (10001–15000 points).

## Likelihood Explanation
The trigger requires: (a) a chain of transactions in the pool exceeding `max_ancestors_count` (default 25), where some members use a specific cell output as a cell dep; (b) a new transaction that spends that cell output as an input and is a descendant of that chain. This is a normal, valid CKB transaction submission path (P2P/RPC `send_transaction`). No special privilege, key, or hashpower is required. An attacker pays fees for ~25 setup transactions per trigger cycle, which is a low cost relative to the persistent mempool disruption achieved. The attack is repeatable and the inflation accumulates.

## Recommendation
Move the `total_tx_size`/`total_tx_cycles` assignment to after `check_and_record_ancestors` returns, computing the final value from the already-updated `self.total_tx_size`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
// self.total_tx_size has already been decremented for any evictions
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
self.total_tx_size = self.total_tx_size
    .checked_add(entry.size)
    .ok_or_else(|| Reject::Full(...))?;
self.total_tx_cycles = self.total_tx_cycles
    .checked_add(entry.cycles)
    .ok_or_else(|| Reject::Full(...))?;
```

The pre-check in `updated_stat_for_add_tx` (overflow guard) can be retained as an early rejection gate, but the actual assignment must use the post-eviction `self.total_tx_size`.

## Proof of Concept
Invariant fuzz/unit test: construct a `PoolMap` with `max_ancestors_count = 2`. Submit transactions T1 (uses cell C as cell dep) and T2 (parent of T1). Submit T3 that spends cell C as an input and has T2 as a parent — this gives `ancestors_count = 3 > 2`, with T1 as a `cell_ref_parent`. `check_and_record_ancestors` evicts T1 (and its descendants). After `add_entry` returns a non-empty `evicts` set, assert:

```rust
assert_eq!(
    pool_map.total_tx_size,
    pool_map.entries.iter().map(|(_, e)| e.inner.size).sum::<usize>()
);
```

This assertion will fail: `total_tx_size` will be inflated by `T1.size` (and any descendants of T1 that were also evicted).

### Citations

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L247-247)
```rust
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
```

**File:** tx-pool/src/component/pool_map.rs (L261-264)
```rust
        removed_ids
            .iter()
            .filter_map(|id| self.remove_entry(id))
            .collect()
```

**File:** tx-pool/src/component/pool_map.rs (L598-628)
```rust
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
```

**File:** tx-pool/src/component/pool_map.rs (L710-728)
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
```
