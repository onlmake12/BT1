The code has been verified against all cited line numbers. The bug is real and confirmed.

**Verification summary:**

- `add_entry` lines 210–211: `updated_stat_for_add_tx` is a `&self` method (confirmed at lines 711–729) — it reads `self.total_tx_size` and returns `self.total_tx_size + tx_size` without mutating state.
- `check_and_record_ancestors` line 213 → eviction branch lines 603–625 → `remove_entry_and_descendants` line 618 → `remove_entry` line 263 → `update_stat_for_remove_tx` line 247: correctly decrements `self.total_tx_size` in place (confirmed at lines 733–758).
- `add_entry` lines 218–219: unconditionally overwrites `self.total_tx_size` with the stale pre-eviction local, discarding the correct decrements.
- `limit_size` line 298: uses `self.pool_map.total_tx_size > self.config.max_tx_pool_size` as the eviction trigger — an inflated counter directly causes premature eviction of legitimate transactions.

All five required checks pass. The exploit path is reachable by an unprivileged user via `send_transaction` RPC. The impact matches the allowed High bounty class.

---

Audit Report

## Title
Stale Pre-Computed `total_tx_size`/`total_tx_cycles` Overwritten After Eviction in `add_entry` Corrupts Pool Accounting - (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, new pool totals are computed into local variables before `check_and_record_ancestors` runs. That function can evict existing entries via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size`/`self.total_tx_cycles`. However, the stale pre-eviction locals are then unconditionally written back, silently discarding the eviction accounting. The result is permanent, cumulative inflation of the pool size counters by the sizes/cycles of every evicted entry, causing `limit_size` to prematurely evict legitimate transactions.

## Finding Description
`PoolMap::add_entry` (lines 200–221) follows this exact sequence:

```rust
// Lines 210-211: captures self.total_tx_size + entry.size into a local BEFORE any evictions
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Line 213: may evict entries; each eviction calls update_stat_for_remove_tx,
// which correctly subtracts from self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

// Lines 218-219: OVERWRITES the correctly-updated self.total_tx_size with the stale value
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` (lines 711–729) is a `&self` method that simply returns `self.total_tx_size + tx_size` without mutating state. The eviction path inside `check_and_record_ancestors` (lines 603–625) calls `remove_entry_and_descendants` (line 618), which calls `remove_entry` (line 263), which calls `update_stat_for_remove_tx` (line 247) — correctly decrementing `self.total_tx_size` in place (lines 733–758). After `check_and_record_ancestors` returns, the correctly-updated `self.total_tx_size` is immediately overwritten with the stale local. Net effect: `self.total_tx_size = original + entry.size` instead of the correct `original − evicted_sizes + entry.size`. The inflation equals the sum of sizes of all evicted entries and is permanent with no self-correcting mechanism.

## Impact Explanation
`total_tx_size` is the authoritative pool-size counter used by `limit_size` (`pool.rs` line 298): `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`. An inflated counter causes `limit_size` to evict legitimate transactions even when the pool has physical room. Since `limit_size` evicts lowest-fee-rate entries first, this degrades pool throughput and reduces effective pool capacity. The inflation is cumulative across every eviction-triggering `add_entry` call, meaning repeated exploitation progressively shrinks the effective pool. This matches the allowed impact: **High — vulnerabilities or bad designs which could cause CKB network congestion with few costs**, as a sufficiently inflated counter causes the pool to continuously reject or evict valid transactions, degrading mempool capacity network-wide.

## Likelihood Explanation
The eviction branch in `check_and_record_ancestors` fires when `ancestors_count > max_ancestors_count` (default 125) but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. An unprivileged attacker must build a chain of ~124 transactions in the pool that also reference a popular cell dep (e.g., secp256k1 lock script), then submit a new transaction referencing the same cell dep. This requires paying fees for ~124 transactions but no special privilege beyond `send_transaction` RPC access. The condition is repeatable: each successful trigger adds more inflation to the counters.

## Recommendation
Move `updated_stat_for_add_tx` (or the final assignment) to **after** `check_and_record_ancestors` completes, so eviction-driven decrements are already reflected in `self.total_tx_size` before the new entry's size is added:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute AFTER evictions have already updated self.total_tx_size
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, remove the locals entirely and perform the increment in-place after all mutations complete.

## Proof of Concept
1. Submit ~124 transactions to the pool that all reference the secp256k1 cell dep, forming a chain (tx1 → tx2 → … → tx124).
2. Submit a new transaction tx125 that (a) spends an output of tx124 and (b) also references the secp256k1 cell dep, pushing `ancestors_count` to 125+.
3. `check_and_record_ancestors` enters the eviction branch (line 603), removes some `cell_ref_parents` via `remove_entry_and_descendants`, and correctly decrements `self.total_tx_size`.
4. `add_entry` then overwrites `self.total_tx_size` with the stale pre-eviction value (lines 218–219).
5. Query `tx_pool_info` via RPC: `total_tx_size` will exceed the sum of all actual entries' sizes.
6. Repeat steps 1–4 multiple times; each iteration inflates the counter further until `limit_size` begins evicting legitimate transactions even though the pool has physical room.
7. A unit test can assert the invariant `pool_map.total_tx_size == pool_map.entries.iter().map(|e| e.size).sum()` before and after a triggered eviction to confirm the discrepancy.