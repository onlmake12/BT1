Audit Report

## Title
Stale Pre-Eviction Snapshot Overwrites In-Place `total_tx_size`/`total_tx_cycles` in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, `updated_stat_for_add_tx` captures the new pool totals into local variables before any evictions occur. `check_and_record_ancestors` may then evict entries via `remove_entry_and_descendants` → `update_stat_for_remove_tx`, which subtracts evicted sizes from `self.total_tx_size`/`self.total_tx_cycles` in-place. The final assignment on lines 218–219 blindly overwrites those in-place values with the stale pre-eviction snapshot, permanently overcounting both fields by the aggregate size/cycles of all evicted entries.

## Finding Description

The exact sequence in `add_entry` (lines 200–221):

1. **Line 210–211**: `updated_stat_for_add_tx` is a `&self` (immutable) method — it returns `(self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles)` into local variables without touching `self`. [1](#0-0) 

2. **Line 213**: `check_and_record_ancestors` is `&mut self` and, when `ancestors_count > max_ancestors_count` but reducible via cell-ref parent eviction (line 603), calls `remove_entry_and_descendants` (line 618) for each evicted entry. That call chain reaches `update_stat_for_remove_tx`, which subtracts the evicted entry's size/cycles from `self.total_tx_size`/`self.total_tx_cycles` in-place. [2](#0-1) [3](#0-2) 

3. **Lines 218–219**: The stale local variables (computed before any eviction) are written back to `self`, erasing every subtraction performed in step 2. [4](#0-3) 

Concrete trace (pool starts at `total_tx_size = 1000`):

| Step | Event | `self.total_tx_size` | local `total_tx_size` |
|------|-------|----------------------|-----------------------|
| 1 | `updated_stat_for_add_tx(size=50)` | 1000 | 1050 |
| 2 | evict entry of size 200 via `update_stat_for_remove_tx` | 800 | 1050 |
| 3 | `self.total_tx_size = total_tx_size` | **1050** (wrong) | — |

Correct value: `800 + 50 = 850`. Actual: `1050`. Overcounted by `200`.

## Impact Explanation

`total_tx_size` is the sole guard in `limit_size`:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size { … }
``` [5](#0-4) 

Each attacker-triggered eviction inflates `total_tx_size` by the evicted entries' aggregate size. `limit_size` then expels that many bytes of legitimate, fee-paying transactions as `Reject::Full`. The overcounting is monotonically additive across repeated attacks: each round permanently raises the apparent pool occupancy, progressively starving the pool of capacity and causing legitimate transactions to be continuously dropped. This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, since an attacker paying only their own transaction fees can force the node to reject third-party transactions indefinitely.

## Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged sender. The attacker needs only to:
1. Seed the pool with transactions sharing a common cell dep (cell-ref parents).
2. Submit a new transaction that (a) references that cell dep and (b) has enough in-pool ancestors to exceed `max_ancestors_count` once cell-ref parents are counted, but satisfies `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`.

No key material, privileged access, or majority hash power is required. The attack is repeatable at the cost of the attacker's own transaction fees. [2](#0-1) 

## Recommendation

Remove the local variable pattern entirely. Instead, validate overflow potential via `updated_stat_for_add_tx` (discarding its return value), allow `check_and_record_ancestors` to perform its in-place subtractions, and then increment `self.total_tx_size`/`self.total_tx_cycles` directly after all evictions complete:

```rust
// Validate no overflow, but discard the snapshot
self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
let evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Increment AFTER evictions have already adjusted self.total_tx_*
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

## Proof of Concept

```
Initial state: total_tx_size=1000, max_tx_pool_size=900
Pool entries: tx_A(size=200, cell_dep_X), tx_B(size=200, cell_dep_X),
              + 24 ancestors of tx_new

Attacker submits tx_new(size=50, cell_dep_X, 25 in-pool ancestors):
  ancestors_count = 26 > max_ancestors_count(25)
  cell_ref_parents = {tx_A, tx_B}
  26 - 2 = 24 <= 25  → eviction path taken

  Step 1: updated_stat_for_add_tx(50) → local=1050, self=1000
  Step 2: evict tx_A(200) → self.total_tx_size = 800
  Step 3: self.total_tx_size = 1050  ← WRONG (should be 850)

limit_size: 1050 > 900 → evicts 150+ bytes of legitimate txs unnecessarily.
Repeat attack: each round adds another 200 bytes of phantom inflation.
```

A unit test can be written against `PoolMap` directly: construct a pool with known `total_tx_size`, insert a transaction that triggers the cell-ref-parent eviction path, and assert `pool_map.total_tx_size == expected_correct_value` after `add_entry` returns.

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
