The code is confirmed. Let me trace the exact execution flow carefully.

Audit Report

## Title
`add_entry` overwrites `total_tx_size`/`total_tx_cycles` with stale pre-computed values after ancestor-eviction decrements them - (File: tx-pool/src/component/pool_map.rs)

## Summary
In `PoolMap::add_entry`, the new pool-size totals are computed into local variables before `check_and_record_ancestors` runs. When `check_and_record_ancestors` evicts existing entries via `remove_entry` → `update_stat_for_remove_tx`, it decrements `self.total_tx_size` and `self.total_tx_cycles` in place. The stale pre-computed locals are then unconditionally written back on lines 218–219, silently discarding those decrements and permanently overcounting the pool's size and cycle usage by the sum of all evicted transactions' sizes/cycles. The inflated totals then cause `limit_size` to evict additional legitimate transactions to compensate for phantom capacity.

## Finding Description

**Exact code path:**

`add_entry` (lines 200–221):
```rust
// Step 1 – snapshot taken BEFORE any eviction
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // line 210-211
//   = self.total_tx_size + entry.size  (local variable)

// Step 2 – may evict existing entries
evicts = self.check_and_record_ancestors(&mut entry)?;          // line 213
//   internally calls remove_entry_and_descendants → remove_entry
//   → update_stat_for_remove_tx, which does:
//       self.total_tx_size -= evicted.size   (in-place mutation)
//       self.total_tx_cycles -= evicted.cycles

// Step 3 – stale snapshot overwrites the decremented live value
self.total_tx_size = total_tx_size;    // line 218  ← BUG
self.total_tx_cycles = total_tx_cycles; // line 219  ← BUG
```

`update_stat_for_remove_tx` (lines 733–758) mutates `self.total_tx_size` and `self.total_tx_cycles` directly. After it runs, the correct live value is `original + entry.size − Σ(evicted_sizes)`. Step 3 instead writes `original + entry.size`, losing the `−Σ(evicted_sizes)` correction.

**Eviction trigger in `check_and_record_ancestors` (lines 603–625):**
The eviction branch fires when:
1. `ancestors_count > max_ancestors_count` (line 598 guard fails), AND
2. `ancestors_count − cell_ref_parents.len() ≤ max_ancestors_count` (line 603 guard passes)

`cell_ref_parents` are existing pool transactions that reference (as cell deps) an output that the new transaction spends as an input (lines 529–534). When both conditions hold, `remove_entry_and_descendants` is called in a loop (line 618) until the ancestor count drops below the limit.

**Why existing guards are insufficient:**
`updated_stat_for_add_tx` (lines 711–729) only checks for overflow; it does not account for subsequent in-place mutations. There is no re-synchronisation of the local snapshot after `check_and_record_ancestors` returns. `limit_size` (line 298) reads `self.pool_map.total_tx_size` directly and evicts until it falls below `max_tx_pool_size`; an inflated value causes it to evict legitimate entries that should remain.

## Impact Explanation
The overcounted `total_tx_size` causes `limit_size` to evict legitimate pending transactions that would otherwise remain in the pool. Each successful trigger of the eviction path in `check_and_record_ancestors` permanently inflates the accounting until an underflow recomputation accidentally corrects it. This maps to **Low (501–2000 points): Any other important performance/correctness improvement for CKB**, as it degrades mempool correctness and can cause valid user transactions to be silently dropped.

## Likelihood Explanation
An unprivileged user can trigger this by: (a) placing transactions in the pool that reference a specific unspent output as a cell dep, and (b) submitting a new transaction that spends that output as an input while also having a deep ancestor chain (> `max_ancestors_count`, default 25). Both conditions are achievable with normal transaction submission APIs. The scenario is narrow but fully within the reach of a motivated attacker with modest on-chain resources.

## Recommendation
Recompute the totals **after** `check_and_record_ancestors` returns rather than before it runs, or re-derive the final values from the live fields at the point of assignment:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute totals AFTER all mutations are complete
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, move the `updated_stat_for_add_tx` call to after `check_and_record_ancestors` so the snapshot is taken from the already-decremented live values.

## Proof of Concept
1. Create a `PoolMap` with `max_ancestors_count = 3`.
2. Insert a chain of 3 transactions (tx_A → tx_B → tx_C) where tx_C produces output O.
3. Insert tx_D that uses output O as a **cell dep** (making tx_C a `cell_ref_parent` of any future tx spending O).
4. Build a chain tx_E → tx_F → tx_G → tx_H where tx_H spends output O as an **input** (ancestor count = 4 > 3, but `cell_ref_parents = {tx_C}` so `4 − 1 = 3 ≤ 3`).
5. Call `pool_map.add_entry(tx_H_entry, Status::Pending)`.
6. Assert: `pool_map.total_tx_size` should equal the sum of sizes of all remaining entries. With the bug, it will be larger by `size(tx_C) + size(tx_D)` (the evicted entries).
7. Confirm `limit_size` then evicts an additional legitimate entry to compensate for the phantom size.