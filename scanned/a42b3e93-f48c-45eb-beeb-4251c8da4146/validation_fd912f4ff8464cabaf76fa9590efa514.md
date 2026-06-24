The code confirms the claim. `updated_stat_for_add_tx` takes `&self` (immutable) and returns computed values without modifying `self`. [1](#0-0)  Then `check_and_record_ancestors` can call `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size`/`self.total_tx_cycles` in place. [2](#0-1)  But lines 218–219 unconditionally overwrite those correctly-decremented fields with the stale snapshot. [3](#0-2) 

---

Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Overwritten With Stale Pre-Computed Values After Eviction in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::add_entry` snapshots updated size/cycle totals before calling `check_and_record_ancestors`, which may evict pool entries via `remove_entry_and_descendants`. Each eviction correctly decrements `self.total_tx_size` and `self.total_tx_cycles` through `update_stat_for_remove_tx`. However, `add_entry` then unconditionally overwrites both fields with the stale pre-eviction snapshot, erasing the eviction decrements and inflating both accounting fields by the aggregate size and cycles of all evicted transactions. The inflated `total_tx_size` causes `limit_size` to over-evict legitimate pending transactions, and the drift compounds with each subsequent triggering submission.

## Finding Description
`updated_stat_for_add_tx` (line 711) takes `&self` and returns `(self.total_tx_size + tx_size, self.total_tx_cycles + cycles)` without modifying `self`. The returned values are captured at lines 210–211 before any evictions occur.

`check_and_record_ancestors` (line 588) enters the eviction branch when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (line 603). It calls `remove_entry_and_descendants` (line 618), which calls `remove_entry` (line 235), which calls `update_stat_for_remove_tx` (line 247), correctly decrementing `self.total_tx_size` and `self.total_tx_cycles` in place.

Lines 218–219 then unconditionally assign:
```rust
self.total_tx_size = total_tx_size;   // stale: pre-eviction + entry.size
self.total_tx_cycles = total_tx_cycles; // stale: pre-eviction + entry.cycles
```
This discards the decrements applied by `update_stat_for_remove_tx`, inflating both totals by the sum of sizes and cycles of all evicted entries.

`limit_size` (line 298) loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, so the inflated value causes it to evict additional legitimate transactions beyond what the true pool occupancy requires. Because each subsequent `add_entry` call that triggers the eviction path reads the already-inflated `self.total_tx_size` as its baseline for `updated_stat_for_add_tx`, the drift compounds monotonically.

The `track_entry_statics` assertion (line 681) checks only entry counts, not size/cycle totals, so it does not detect this corruption.

## Impact Explanation
An attacker can repeatedly trigger this path to drive `total_tx_size` arbitrarily above the true pool occupancy. Once the inflation exceeds `max_tx_pool_size`, `limit_size` will evict every newly submitted transaction immediately upon insertion, even when the real pool is nearly empty. This constitutes a low-cost, repeatable denial-of-service against the mempool: legitimate transactions cannot remain in the pool, miner revenue is degraded, and normal transaction propagation across the P2P network is disrupted. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation
The trigger is reachable by any unprivileged user via the `send_transaction` RPC or P2P relay. The attacker needs only to: (1) submit a transaction T that uses a live cell C0 as a cell dep; (2) build a chain of `max_ancestors_count` (default 25) transactions rooted at a transaction spending C0 as an input; (3) submit a final transaction spending C0 as an input with that chain as ancestors (giving it 26 ancestors, one over the limit, with `cell_ref_parents = {T}`). No privileged access, no majority hashpower, and no leaked keys are required. The attack is repeatable and each repetition compounds the inflation.

## Recommendation
Move the stat update to after all evictions have completed, operating directly on the live `self` fields rather than a pre-eviction snapshot:

```rust
// Remove the pre-eviction snapshot entirely.
// After check_and_record_ancestors returns, self.total_tx_size/cycles
// already reflect all eviction decrements; just add the new entry's contribution.
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Overflow check + update after evictions are complete.
self.total_tx_size = self.total_tx_size.checked_add(entry.size)
    .ok_or_else(|| Reject::Full(...))?;
self.total_tx_cycles = self.total_tx_cycles.checked_add(entry.cycles)
    .ok_or_else(|| Reject::Full(...))?;
```

The overflow pre-check can be retained as an early guard (returning `Err` before any mutations) but must not capture values that are later unconditionally written back.

## Proof of Concept
1. Start a node with default `max_ancestors_count = 25`.
2. Submit transaction **T** spending live cell `C0` as a **cell dep**. Record `T.size`.
3. Submit chain `A1 → A2 → … → A25` where `A1` spends `C0` as an **input**.
4. Submit transaction **N** with inputs `{C0, output-of-A25}` (26 ancestors, `cell_ref_parents = {T}`).
5. Observe via `tx_pool_info` RPC that `total_tx_size` is inflated by `T.size` relative to the true sum of entries.
6. Repeat steps 2–4 with fresh cells. Each iteration inflates `total_tx_size` by another `T.size`.
7. After enough iterations, `total_tx_size` exceeds `max_tx_pool_size`; `limit_size` begins evicting every newly submitted transaction, confirming the DoS condition.

A unit test can assert `pool_map.total_tx_size == pool_map.entries.iter().map(|e| e.size).sum()` after step 4 to directly observe the invariant violation.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L247-247)
```rust
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
```

**File:** tx-pool/src/component/pool_map.rs (L711-729)
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
    }
```
