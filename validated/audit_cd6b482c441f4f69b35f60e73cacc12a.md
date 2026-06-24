Audit Report

## Title
Stale Pre-Eviction Snapshot Overwrites Live `total_tx_size`/`total_tx_cycles` Counters in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, new pool-size totals are captured into local variables before `check_and_record_ancestors` runs. When that function evicts transactions via `remove_entry_and_descendants` → `update_stat_for_remove_tx`, it correctly decrements `self.total_tx_size`/`self.total_tx_cycles` in place. Lines 218–219 then unconditionally overwrite those live values with the stale pre-eviction locals, permanently cancelling every decrement. Repeated exploitation inflates the counters without bound, causing `limit_size` to continuously evict legitimate transactions and reject all new submissions with `Reject::Full`.

## Finding Description
`add_entry` (lines 200–221) executes this sequence:

1. **Lines 210–211**: `updated_stat_for_add_tx(&self, ...)` is a read-only borrow. It computes `self.total_tx_size + entry.size` and `self.total_tx_cycles + entry.cycles` into local variables `total_tx_size` and `total_tx_cycles`, returning them without writing to `self`.

2. **Line 213**: `check_and_record_ancestors(&mut self, ...)` may enter the eviction branch (lines 603–625) when `ancestors_count > max_ancestors_count` and `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. Inside the `while` loop (line 616), it calls `self.remove_entry_and_descendants(next_id)`, which internally calls `update_stat_for_remove_tx` (lines 733–758). That function **writes** the decremented values directly to `self.total_tx_size` and `self.total_tx_cycles`.

3. **Lines 218–219**: The stale locals captured in step 1 are unconditionally assigned back:
   ```rust
   self.total_tx_size = total_tx_size;   // pre-eviction snapshot
   self.total_tx_cycles = total_tx_cycles; // pre-eviction snapshot
   ```
   This overwrites the correctly-decremented live values, effectively adding back the size/cycles of every evicted transaction.

Existing guards are insufficient: `updated_stat_for_add_tx` only checks for integer overflow before eviction occurs, and `update_stat_for_remove_tx`'s underflow fallback (`recompute_total_stat`) is never reached because the overwrite happens after it has already correctly updated the counters.

## Impact Explanation
`total_tx_size` is the sole guard in `limit_size` (pool.rs line 298):
```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size { ... }
```
Each exploiting submission inflates `total_tx_size` by the combined size of all evicted transactions. Repeated submissions drive the counter to `max_tx_pool_size` while the actual pool remains nearly empty. Once saturated, `limit_size` fires on every subsequent `add_entry`, evicting all legitimate fee-paying transactions and returning `Reject::Full` to all new submissions. This constitutes **CKB network congestion with few costs**, matching the **High** impact tier (10001–15000 points).

## Likelihood Explanation
The trigger condition is fully attacker-controlled: submit a transaction whose ancestor set contains cell-ref parents that push `ancestors_count` just over `max_ancestors_count`. No privileged access, no majority hashpower, and no victim mistakes are required. The entry path is the standard `send_transaction` RPC / P2P relay path. The attack is repeatable with fresh transactions, allowing unbounded counter inflation at low cost.

## Recommendation
Compute the new totals **after** all evictions have completed. Replace the pre-eviction snapshot with a post-eviction addition:

```rust
// Validate capacity (read-only check) — still needed to reject if already full
self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Apply addition AFTER evictions have already decremented the counters
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

This ensures the addition is applied to the post-eviction state rather than a stale pre-eviction snapshot.

## Proof of Concept
**Setup:** `max_ancestors_count = 25`, `max_tx_pool_size = 1_000_000` bytes.

1. Submit base transaction `T0` creating output `O`.
2. Submit 24 transactions `T1…T24`, each referencing `O` as a cell dep (~1 000 bytes each). Pool: 25 entries, `total_tx_size ≈ 25_000`.
3. Submit `Tnew` (1 000 bytes) spending an output of `T0`. Its ancestor set includes `T1…T24` via cell-dep linkage → `ancestors_count = 26 > 25`. `cell_ref_parents = {T1…T24}`, so `26 - 24 = 2 ≤ 25` — eviction branch taken.
4. `check_and_record_ancestors` evicts e.g. `T1`, `T2` (each 1 000 bytes). `update_stat_for_remove_tx` correctly sets `self.total_tx_size = 23_000`.
5. Line 218 writes back stale snapshot: `self.total_tx_size = 26_000` (should be `24_000`). Inflation: **+2 000 bytes per call**.
6. Repeat ~490 times. `total_tx_size` reaches `≈ 1_000_000` while actual pool is nearly empty. `limit_size` fires on every subsequent `add_entry`, evicting all legitimate transactions and returning `Reject::Full` to all new submissions.

A unit test can be written against `PoolMap::add_entry` directly: assert that after an eviction-triggering `add_entry`, `pool_map.total_tx_size` equals the sum of sizes of all entries actually present in `pool_map.entries`.