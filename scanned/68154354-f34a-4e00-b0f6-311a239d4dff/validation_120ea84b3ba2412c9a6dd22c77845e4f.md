Audit Report

## Title
Stale Pre-Computed Pool Counters Overwrite Eviction-Adjusted Totals, Inflating `total_tx_size`/`total_tx_cycles` — (`tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, aggregate size and cycle counters are pre-computed before `check_and_record_ancestors` runs. That function can evict existing entries via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size`/`self.total_tx_cycles` in place. However, `add_entry` then unconditionally overwrites those correctly-decremented fields with the stale pre-eviction snapshot, permanently inflating both counters by the aggregate size/cycles of every evicted entry. Every subsequent `add_entry` call reads the inflated baseline, making the inflation cumulative and irrecoverable without a node restart.

## Finding Description
The exact sequence in `add_entry` (lines 210–219):

```rust
// Step 1: snapshot pre-eviction totals + new entry
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;  // L210-211

// Step 2: may evict entries; each eviction calls update_stat_for_remove_tx,
//         which DECREMENTS self.total_tx_size / self.total_tx_cycles in place
evicts = self.check_and_record_ancestors(&mut entry)?;        // L213

// Step 3: OVERWRITES the correctly-decremented live counters
//         with the stale pre-eviction snapshot
self.total_tx_size = total_tx_size;                           // L218
self.total_tx_cycles = total_tx_cycles;                       // L219
```

The eviction path inside `check_and_record_ancestors` (lines 603–625) is triggered when `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. Each evicted entry goes through `remove_entry` → `update_stat_for_remove_tx` (line 247), which modifies `self.total_tx_size` and `self.total_tx_cycles` in place (lines 738–740). Step 3 then discards those decrements entirely.

If evicted entries have aggregate size `S_evicted`, the post-`add_entry` value of `self.total_tx_size` is `old_total + entry.size` instead of the correct `old_total - S_evicted + entry.size`. The inflation is exactly `S_evicted` per exploit iteration.

The developers themselves acknowledge the eviction scenario is real and that rollback is not handled, via the `FIXME` comment at lines 582–587.

`updated_stat_for_add_tx` (lines 711–729) uses `self.total_tx_size` and `self.total_tx_cycles` as the baseline for computing new totals and for overflow/capacity checks. Once inflated, every subsequent call reads the inflated baseline, compounding the error. The underflow recovery in `update_stat_for_remove_tx` (lines 742–756) recomputes from actual entries only on underflow, and does not correct inflation in the global counter.

## Impact Explanation
`total_tx_size` and `total_tx_cycles` are the primary accounting fields used for pool admission and capacity reporting. Persistent inflation of these counters causes the pool to report itself as fuller than it actually is. Once the inflated value crosses the configured pool capacity threshold, every subsequent `add_entry` call is rejected, permanently blocking all new transaction submissions to the node's tx-pool. The node continues to sync blocks but cannot participate in transaction relay or block assembly — a node-level DoS on transaction ingestion. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, since an attacker paying only minimum fees can permanently disable a node's tx-pool.

## Likelihood Explanation
The eviction path requires an attacker to construct a cell-dep ancestor chain of length ≥ `max_ancestors_count` (default 25) and submit a transaction that references one of those ancestors as a cell dep, satisfying `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. This is achievable by any unprivileged tx-pool submitter via RPC `send_transaction` or P2P relay, paying only minimum fees. The exploit is repeatable: each iteration inflates the counters by `S_evicted`, and the inflation accumulates until the pool is permanently closed. No special privileges, leaked keys, or victim mistakes are required.

## Recommendation
Remove the pre-computation before `check_and_record_ancestors`. After all evictions complete and the new entry is inserted, apply the delta atomically on the post-eviction baseline:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Apply delta on the post-eviction baseline, not a pre-eviction snapshot:
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, validate pool capacity limits after evictions complete rather than before, so the capacity check always operates on the true post-eviction state.

## Proof of Concept
1. Configure a node with `max_ancestors_count = 25` (default).
2. Submit 25 transactions forming a linear chain `tx_0 → tx_1 → … → tx_24`. All are accepted; `total_tx_size` = sum of their sizes.
3. Submit `tx_A` that spends an output of `tx_0` **and** uses `tx_12` as a cell dep. `tx_A`'s ancestor set includes `tx_0`…`tx_24` (26 > 25). The cell-dep path satisfies `26 - 1 <= 25`, triggering eviction of `tx_12` and its descendants `tx_13`…`tx_24` (13 entries, aggregate size `S_evicted`).
4. `check_and_record_ancestors` calls `remove_entry_and_descendants(&tx_12_id)`, which removes 13 entries and decrements `self.total_tx_size` by `S_evicted`.
5. `add_entry` then writes `self.total_tx_size = old_total + tx_A.size` (the pre-eviction snapshot), restoring the inflated value. `total_tx_size` is now inflated by `S_evicted`.
6. Repeat steps 2–5 until `total_tx_size` exceeds the pool capacity threshold.
7. All subsequent `send_transaction` RPC calls are rejected even though the pool contains far fewer bytes than the configured limit.

A targeted unit test can instrument `PoolMap` directly: assert that after a successful `add_entry` that triggers eviction, `pool_map.total_tx_size` equals the sum of sizes of all remaining entries (verifiable via `recompute_total_stat`). The current code will fail this invariant.