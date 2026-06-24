Audit Report

## Title
Stale Pre-Computed `total_tx_size`/`total_tx_cycles` Overwrites Post-Eviction Decrements in `add_entry` — (`tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, pool-size totals are snapshotted via `updated_stat_for_add_tx` at lines 210–211 before `check_and_record_ancestors` runs. When that function evicts transactions through `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx` (line 247), the live counters are correctly decremented. However, the stale pre-eviction snapshot is then unconditionally written back at lines 218–219, silently discarding every eviction decrement. Each eviction event permanently inflates `total_tx_size` and `total_tx_cycles` by the aggregate size/cycles of the evicted transactions, eventually causing all subsequent `add_entry` calls to return `Reject::Full`.

## Finding Description

The exact sequence in `add_entry` (lines 200–221):

```rust
// Step 1 — snapshot BEFORE any evictions (self.total_tx_size = X)
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // L210-211
    // total_tx_size = X + entry.size

// Step 2 — may evict via remove_entry → update_stat_for_remove_tx
//           which DECREMENTS self.total_tx_size to X - evicted_size
evicts = self.check_and_record_ancestors(&mut entry)?;          // L213

// Step 3 — OVERWRITES the correctly-decremented live counter
self.total_tx_size  = total_tx_size;   // L218: writes X + entry.size, not X - evicted_size + entry.size
self.total_tx_cycles = total_tx_cycles; // L219
```

The eviction path inside `check_and_record_ancestors` (lines 603–625) is triggered when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. In that case, the lowest-fee `cell_ref_parents` are removed via `remove_entry_and_descendants`, each of which calls `remove_entry` → `update_stat_for_remove_tx` at line 247, correctly decrementing `self.total_tx_size` and `self.total_tx_cycles`.

Those decrements are immediately erased when `add_entry` writes back the stale snapshot at lines 218–219. After each eviction event, `total_tx_size` is inflated by exactly the sum of the evicted transactions' sizes, and `total_tx_cycles` by their cycles. The inflation is monotonically cumulative across repeated submissions.

`updated_stat_for_add_tx` uses `checked_add` and returns `Reject::Full` on integer overflow. `total_tx_size` is also compared against the configured `max_tx_pool_size` limit in `pool.rs`. Once the inflated counter exceeds either bound, every subsequent `add_entry` returns `Reject::Full`, freezing the pool.

The FIXME comment at line 583 acknowledges that rollback of eviction-then-failure is not handled, confirming this is a known structural gap.

## Impact Explanation

`total_tx_size` and `total_tx_cycles` are the authoritative pool-capacity counters. After one or more eviction events, these counters diverge upward from reality. The pool enforces a phantom size larger than the actual bytes/cycles it holds. All subsequent legitimate transactions are rejected with `Reject::Full` even though the pool has physical capacity. This constitutes **CKB network congestion with few costs** — matching the **High (10001–15000 points)** impact class.

## Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged transaction submitter. No privileged keys, majority hashpower, or social engineering are required. The attacker only needs to submit valid transactions that share a common cell dep (making them `cell_ref_parents` of each other), then submit a new transaction whose ancestor count exceeds `max_ancestors_count` but whose excess is covered by those `cell_ref_parents`. This is a standard, valid transaction submission flow. Repeated triggering accumulates inflation monotonically. The attack cost is proportional to the number of transactions submitted, which is low relative to the impact of freezing a node's tx-pool.

## Recommendation

Move the stat update to **after** `check_and_record_ancestors` completes, computing the delta relative to the post-eviction live counters rather than pre-computing against the pre-eviction snapshot. Replace the pre-computed snapshot pattern with an incremental add applied only after all evictions have settled:

```rust
// Remove lines 210-211 (the pre-computation)
// After check_and_record_ancestors, record_entry_edges, insert_entry, etc.:
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

The overflow/capacity check (`updated_stat_for_add_tx`) should be moved to after evictions complete, operating on the post-eviction `self.total_tx_size` value, so it accurately reflects available capacity.

## Proof of Concept

**Setup:** Configure a node with `max_ancestors_count = 25`. Submit 25 transactions `A₁…A₂₅` that all reference the same cell dep `C` (making them mutual `cell_ref_parents`). Let the pool's `total_tx_size` be `X` and each `Aᵢ` have size `S`.

**Attack loop (repeat N times):**
1. Submit a new transaction `T` that spends an output of `A₁` (making `A₁` a parent/ancestor of `T`) and also references cell dep `C`.
2. `check_and_record_ancestors` sees `ancestors_count = 26 > 25`, but `cell_ref_parents = {A₁…A₂₅}`, so `26 - 25 = 1 ≤ 25`. It evicts `A₁` (lowest fee), calling `update_stat_for_remove_tx(A₁.size, A₁.cycles)`, decrementing `self.total_tx_size` by `S`.
3. `add_entry` writes back `total_tx_size = X + T.size` (the stale snapshot), re-inflating by `S`.
4. After `N` iterations, `total_tx_size` is inflated by `N × S` above reality.

**Result:** Once the inflated `total_tx_size` exceeds `max_tx_pool_size` (default 180 MB), `updated_stat_for_add_tx` returns `Reject::Full` for every subsequent submission, freezing the pool until node restart. A unit test can verify this by asserting `pool_map.total_tx_size` after each eviction cycle and confirming it diverges from `pool_map.recompute_total_stat()`.