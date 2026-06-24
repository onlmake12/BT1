Audit Report

## Title
Inflated `total_tx_size`/`total_tx_cycles` Counters Due to Pre-Eviction Snapshot Overwrite in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, the updated pool size and cycles totals are computed into local variables **before** ancestor-eviction side-effects occur, then unconditionally written back to the struct fields **after** those evictions have already decremented the counters. This permanently inflates `total_tx_size` and `total_tx_cycles` by the aggregate size/cycles of all evicted transactions. An inflated `total_tx_size` causes `TxPool::limit_size` to believe the pool is over-capacity and evict legitimate transactions that should have been retained.

## Finding Description

The exact code sequence in `add_entry` (lines 210–219) is confirmed in the repository:

```rust
// Step 1: snapshot pre-eviction totals into locals
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;  // L210-211

// Step 2: may evict N transactions; each calls update_stat_for_remove_tx,
//         which DECREMENTS self.total_tx_size and self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;         // L213

// ... insert, link, track ...

// Step 3: OVERWRITES struct fields with the pre-eviction snapshot,
//         discarding all decrements applied in Step 2
self.total_tx_size = total_tx_size;                            // L218
self.total_tx_cycles = total_tx_cycles;                        // L219
```

`updated_stat_for_add_tx` (L711–728) reads `self.total_tx_size` at call time and returns `self.total_tx_size + entry.size` as a local — before any evictions occur.

`check_and_record_ancestors` (L588–640) calls `remove_entry_and_descendants` in a loop when `ancestors_count > max_ancestors_count` and the excess is attributable to cell-ref parents. Each removed entry triggers `update_stat_for_remove_tx` (L733–758), which directly decrements `self.total_tx_size` and `self.total_tx_cycles` on the struct.

After all evictions complete, lines 218–219 blindly restore the pre-eviction snapshot, erasing every decrement. The net result: `total_tx_size` is inflated by exactly the sum of the evicted transactions' sizes, and `total_tx_cycles` by their cycles.

## Impact Explanation

`total_tx_size` is the sole counter driving the pool-eviction loop in `TxPool::limit_size` (L298):

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size { ... }
```

An inflated counter causes `limit_size` to believe the pool is over-capacity and evict additional legitimate pending/gap/proposed transactions that should have been retained. This constitutes a **tx-pool denial-of-service**: honest users' transactions are expelled from the pool even though real pool occupancy is within limits. The inflation accumulates across repeated attacks and persists until node restart or pool clear.

This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**. The attacker pays fees for submitted transactions but can repeatedly inflate the counter, causing ongoing eviction of honest transactions and degrading network usability.

## Likelihood Explanation

The eviction path in `check_and_record_ancestors` is triggered when a submitted transaction has more than `max_ancestors_count` (default 1000) ancestors, and the excess is due to cell-ref parents. Any unprivileged tx-pool submitter can engineer this:

1. Submit ≥1001 transactions each carrying a `cell_dep` pointing to a specific live cell (e.g., a widely-used lock script output).
2. Submit one transaction that **consumes** that cell as an input.

Step 2 triggers the eviction loop. The integration test `TxPoolLimitAncestorCount` (test/src/specs/tx_pool/limit.rs, L70–157) demonstrates exactly this scenario with 2000 cell-ref transactions, confirming the path is reachable and exercised. The attack is repeatable with each new batch of cell-ref transactions.

## Recommendation

Compute the new totals **after** all evictions have been applied. Remove the pre-eviction snapshot entirely and instead increment the post-eviction baseline:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Add new entry's contribution to the already-eviction-adjusted counters:
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, retain the overflow-check semantics of `updated_stat_for_add_tx` but call it **after** `check_and_record_ancestors` returns, so it operates on the post-eviction baseline.

## Proof of Concept

**Setup:** pool with `max_ancestors_count = 1000`, `max_tx_pool_size = 180_000_000`.

1. Submit 1001 transactions `T1…T1001`, each with `cell_dep` pointing to cell `C`. Each ~200 bytes. `self.total_tx_size ≈ 200_200`.
2. Submit `T_consume` spending cell `C` as input (~200 bytes). `updated_stat_for_add_tx` captures `total_tx_size_local = 200_400`.
3. `check_and_record_ancestors` finds `ancestors_count = 1002 > 1000`. Evicts 2 lowest-fee cell-ref parents via `remove_entry_and_descendants`, each calling `update_stat_for_remove_tx`. `self.total_tx_size` decremented by ~400 bytes → `self.total_tx_size ≈ 199_800`.
4. Lines 218–219 restore the snapshot: `self.total_tx_size = 200_400`.
5. **Inflation = ~400 bytes** (evicted transactions double-counted).

Repeating with larger batches (as in `TxPoolLimitAncestorCount` with 2000 txs, evicting 1002) inflates by the sum of all evicted sizes per invocation. Once accumulated inflation exceeds `max_tx_pool_size`, `limit_size` begins evicting honest transactions on every subsequent submission.