Audit Report

## Title
Stale Pre-Eviction Snapshot Overwrites In-Place `total_tx_size`/`total_tx_cycles` in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, `updated_stat_for_add_tx` (a `&self` method) computes and returns the post-add totals into local variables before any evictions occur. When `check_and_record_ancestors` subsequently evicts entries via `remove_entry_and_descendants` → `update_stat_for_remove_tx`, those subtractions are applied in-place to `self.total_tx_size`/`self.total_tx_cycles`. The final assignment on lines 218–219 then blindly overwrites those corrected in-place values with the stale pre-eviction snapshot, permanently overcounting both fields by the aggregate size/cycles of all evicted entries.

## Finding Description

The exact sequence in `add_entry` (lines 200–221):

**Step 1 — Snapshot captured (lines 210–211):**
`updated_stat_for_add_tx` is a `&self` method. It computes `self.total_tx_size.checked_add(tx_size)` and `self.total_tx_cycles.checked_add(cycles)` and returns them as a tuple without modifying `self`. [1](#0-0) 

The returned values are stored in locals `total_tx_size` and `total_tx_cycles`: [2](#0-1) 

**Step 2 — In-place subtractions during eviction (line 213):**
`check_and_record_ancestors` is `&mut self`. When `ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count` (line 603), it enters the eviction loop and calls `remove_entry_and_descendants` for each evicted entry: [3](#0-2) 

`remove_entry_and_descendants` calls `update_stat_for_remove_tx`, which subtracts the evicted entry's size/cycles directly from `self.total_tx_size`/`self.total_tx_cycles`: [4](#0-3) 

**Step 3 — Stale snapshot overwrites corrected values (lines 218–219):**
After all evictions have already adjusted `self.total_tx_size`/`self.total_tx_cycles` downward, the stale locals (computed before any eviction) are written back unconditionally: [5](#0-4) 

Every subtraction performed in step 2 is erased. The result is that `self.total_tx_size` ends up as `original_total + new_entry_size` regardless of how many bytes were evicted, overcounting by the aggregate size of all evicted entries.

Concrete trace (pool starts at `total_tx_size = 1000`):

| Step | Event | `self.total_tx_size` | local `total_tx_size` |
|------|-------|----------------------|-----------------------|
| 1 | `updated_stat_for_add_tx(size=50)` | 1000 | 1050 |
| 2 | evict entry of size 200 via `update_stat_for_remove_tx` | 800 | 1050 |
| 3 | `self.total_tx_size = total_tx_size` | **1050** (wrong) | — |

Correct value: `800 + 50 = 850`. Actual: `1050`. Overcounted by `200`.

## Impact Explanation

`total_tx_size` is the sole guard in `limit_size`: [6](#0-5) 

Each attacker-triggered eviction inflates `total_tx_size` by the evicted entries' aggregate size. `limit_size` then expels that many bytes of legitimate, fee-paying transactions as `Reject::Full`. The overcounting is monotonically additive: each attack round permanently raises the apparent pool occupancy, progressively starving the pool of capacity and causing legitimate transactions to be continuously dropped.

This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, since an attacker paying only their own transaction fees can force the node to reject third-party transactions indefinitely.

## Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged sender. The attacker needs only to:
1. Seed the pool with transactions sharing a common cell dep (cell-ref parents).
2. Submit a new transaction that (a) references that cell dep and (b) has enough in-pool ancestors to exceed `max_ancestors_count` once cell-ref parents are counted, but satisfies `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`.

No key material, privileged access, or majority hash power is required. The attack is repeatable at the cost of the attacker's own transaction fees, with each round adding another phantom inflation increment to `total_tx_size`. [3](#0-2) 

## Recommendation

Remove the local variable pattern entirely. Allow `check_and_record_ancestors` to perform its in-place subtractions, then increment `self.total_tx_size`/`self.total_tx_cycles` directly after all evictions complete:

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

This requires changing `updated_stat_for_add_tx` to only perform the overflow check (returning `Result<(), Reject>`) or keeping it as-is and discarding the return value.

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

A unit test can be written directly against `PoolMap`: construct a pool with known `total_tx_size`, insert a transaction that triggers the cell-ref-parent eviction path in `check_and_record_ancestors`, and assert `pool_map.total_tx_size == expected_correct_value` after `add_entry` returns. [7](#0-6)

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

**File:** tx-pool/src/component/pool_map.rs (L711-728)
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
