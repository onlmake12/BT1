Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Stale Snapshot Overwrite in `add_entry` Causes Inflated Pool Stats and Spurious Eviction — (`File: tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::add_entry` pre-computes `total_tx_size` and `total_tx_cycles` into local variables before calling `check_and_record_ancestors`, which can internally evict entries via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, correctly decrementing the counters in-place. The pre-computed stale snapshot is then unconditionally written back, erasing those decrements. The result is a persistent upward drift in `total_tx_size` and `total_tx_cycles` that causes `limit_size` to expel legitimate pool entries that should not have been removed.

## Finding Description
In `add_entry` (L200–221), the stat snapshot is taken at L210–211 before any eviction:

```rust
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;  // Step A
evicts = self.check_and_record_ancestors(&mut entry)?;         // Step B – may evict
// ...
self.total_tx_size = total_tx_size;   // Step C – stale write-back
self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` (L711–729) captures `self.total_tx_size + entry.size` into a local at the moment of the call. `check_and_record_ancestors` (L588–640) enters the eviction branch at L603 when `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`, calling `remove_entry_and_descendants` (L618). That function calls `remove_entry` (L235–250), which calls `update_stat_for_remove_tx` at L247, subtracting the evicted entry's size and cycles from `self.total_tx_size`/`self.total_tx_cycles` in-place (L733–758). At Step C, the stale locals (computed before eviction) are written back, erasing those decrements. After `add_entry` returns, `self.total_tx_size` equals `original_total + entry.size` instead of the correct `original_total - evicted_size + entry.size`. The `recompute_total_stat` fallback (L743) is only triggered on underflow, which never occurs here since the inflated value is always positive. `limit_size` (L292–329) reads `self.pool_map.total_tx_size` directly at L298 and evicts entries until it drops below `max_tx_pool_size`, so the inflated counter causes it to expel valid transactions.

## Impact Explanation
An unprivileged attacker can repeatedly trigger the eviction branch to accumulate drift in `total_tx_size`, causing `limit_size` to continuously over-evict legitimate, fee-paying transactions from the mempool. This allows targeted displacement of victim transactions without legitimately filling the pool, degrading mempool fairness and enabling low-cost disruption of transaction propagation across the network. This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, since the attacker can selectively drain the mempool of honest transactions with a small, repeatable, unprivileged construction.

## Likelihood Explanation
The eviction branch in `check_and_record_ancestors` is reachable by any `send_transaction` RPC caller. The attacker must construct a transaction whose ancestor count exceeds `max_ancestors_count` (default 25) but whose excess ancestors are all cell-ref parents (pool entries sharing a cell dep with the new transaction). This is fully attacker-controlled: pre-populate the pool with a chain of transactions sharing a cell dep, then submit a transaction referencing that dep with a long ancestor chain. No privileged access, key material, or majority hash power is required. The construction is repeatable, and drift accumulates with each iteration.

## Recommendation
Move the stat update to after `check_and_record_ancestors` completes, so it reflects the post-eviction state:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Apply increment only after all evictions are done
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, replace the pre-computed snapshot pattern entirely with in-place increments applied after `check_and_record_ancestors`, mirroring how `update_stat_for_remove_tx` applies decrements in-place.

## Proof of Concept
1. Start a node with `max_tx_pool_size = 200_000` bytes, `max_ancestors_count = 25`.
2. Submit 25 transactions `T1 → T2 → … → T25` (a chain), each referencing a shared cell dep `C`. All are accepted; `total_tx_size = 25 * S`.
3. Submit `T26` spending `T25`'s output and also referencing `C`. Ancestor count = 26 > 25. Since `T1…T25` are cell-ref parents, the eviction branch fires: `T1` is removed via `remove_entry_and_descendants`; `update_stat_for_remove_tx` decrements `self.total_tx_size` to `24 * S`.
4. Step C in `add_entry` writes back the stale snapshot: `self.total_tx_size = 25 * S + T26.size`. Actual pool holds 25 entries with true size `25 * S`, but the counter reads `25 * S + T26.size`.
5. `limit_size` is called. It sees `total_tx_size > max_tx_pool_size` (if pool is near capacity) and evicts the next lowest-fee-rate entry — a victim transaction the attacker did not pay to displace.
6. Repeating steps 2–5 accumulates drift of `T26.size` per iteration, enabling continuous over-eviction of the pool.

A unit test can assert that after calling `add_entry` with a transaction that triggers the eviction branch, `pool_map.total_tx_size` equals the sum of sizes of all entries actually remaining in the pool (verifiable via `recompute_total_stat`).