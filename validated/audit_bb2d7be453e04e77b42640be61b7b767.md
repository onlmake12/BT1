Audit Report

## Title
Stale Pre-Computed `total_tx_size`/`total_tx_cycles` Overwrites In-Place Updates After Ancestor Eviction — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` snapshots future totals before `check_and_record_ancestors` runs. When the eviction branch inside `check_and_record_ancestors` fires, `remove_entry` → `update_stat_for_remove_tx` directly mutates `self.total_tx_size` and `self.total_tx_cycles` in-place. Lines 218–219 then unconditionally overwrite those fields with the stale pre-eviction snapshot, permanently inflating both counters. The inflated `total_tx_size` causes `limit_size` to loop and evict valid, fee-paying transactions that would otherwise remain in the pool.

## Finding Description
**Root cause — `add_entry` lines 200–221:**

```rust
// Step 1: snapshot BEFORE eviction
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // L210-211

// Step 2: may call remove_entry_and_descendants → remove_entry
//         → update_stat_for_remove_tx, which DIRECTLY MUTATES
//         self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;          // L213

// Step 3: OVERWRITES the in-place mutations from Step 2
self.total_tx_size = total_tx_size;                             // L218
self.total_tx_cycles = total_tx_cycles;                         // L219
```

`updated_stat_for_add_tx` (lines 711–729) is an immutable `&self` method that returns `self.total_tx_size + entry.size` and `self.total_tx_cycles + entry.cycles` as a snapshot.

`check_and_record_ancestors` (lines 588–640) enters the eviction branch at line 603 when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. It calls `remove_entry_and_descendants` → `remove_entry` (line 247), which calls `update_stat_for_remove_tx` — a `&mut self` method that directly subtracts the evicted entry's size and cycles from `self.total_tx_size` and `self.total_tx_cycles` (lines 738–740).

Lines 218–219 then write the stale snapshot back, erasing those subtractions. The result is that `total_tx_size` and `total_tx_cycles` reflect `(pre-eviction pool) + (new entry)` instead of `(post-eviction pool) + (new entry)`.

**Concrete trace (tx A size=100 in pool, tx B size=50 being added):**

| Step | `self.total_tx_size` |
|---|---|
| Before add_entry | 100 |
| After L210 (snapshot) | snapshot=150 |
| After L213 (A evicted via update_stat_for_remove_tx) | 0 |
| After L218 (stale overwrite) | **150** ← wrong |
| Correct (only B in pool) | 50 |

`limit_size` (pool.rs lines 292–329) loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, so the inflated value of 150 triggers eviction of tx B even though the pool is actually under the limit.

## Impact Explanation
The inflated `total_tx_size` causes `limit_size` to continuously evict valid, fee-paying transactions from the mempool. An attacker who repeatedly submits crafted transactions can keep the pool in a permanent eviction loop, preventing honest users' transactions from being accepted or retained. This constitutes **mempool denial-of-service with low cost per submission**, matching the allowed High impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation
The eviction branch at line 603 is reachable by any unprivileged user via `send_transaction` RPC or P2P relay. The attacker needs only to submit a transaction that:
1. References an existing pool transaction as a cell dep (`cell_ref_parents` non-empty), AND
2. Has `ancestors_count > max_ancestors_count` AND `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`

This is a standard, supported transaction pattern. No privileged access, leaked keys, or majority hashpower is required. The inflation compounds monotonically with each triggering submission, making the attack cheap to sustain.

## Recommendation
Move the stat update to after `check_and_record_ancestors` returns, so evictions are already reflected before adding the new entry's contribution:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Add only the new entry's contribution AFTER evictions are settled
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, remove `updated_stat_for_add_tx` entirely and introduce a mutable `update_stat_for_add_tx` that mirrors `update_stat_for_remove_tx`, called after `check_and_record_ancestors`.

## Proof of Concept
Deterministic reasoning (no external tooling required):

1. Pre-populate pool with tx A (size=100, cycles=1000). `total_tx_size = 100`.
2. Submit tx B (size=50, cycles=500) with a cell dep on A's output, with `ancestors_count = max_ancestors_count + 1` and `cell_ref_parents = {A}`, satisfying the branch condition at line 603.
3. `updated_stat_for_add_tx` at L210 snapshots `total_tx_size = 150`.
4. `check_and_record_ancestors` at L213 evicts A via `remove_entry_and_descendants` → `update_stat_for_remove_tx(100, 1000)` → `self.total_tx_size = 0`.
5. L218 sets `self.total_tx_size = 150` (stale snapshot).
6. Pool contains only tx B (size=50) but reports `total_tx_size = 150`.
7. `limit_size` at pool.rs L298 fires if `max_tx_pool_size < 150`, evicting tx B despite the pool being under the real limit.
8. Repeating step 2 with fresh transactions compounds the inflation monotonically.