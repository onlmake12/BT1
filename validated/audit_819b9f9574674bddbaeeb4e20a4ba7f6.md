The code confirms the claim at every step:

- `updated_stat_for_add_tx` is a `&self` (immutable) method that returns a point-in-time snapshot [1](#0-0) 
- Lines 210–211 store that snapshot in locals before any mutation occurs [2](#0-1) 
- `check_and_record_ancestors` (L603–625) calls `remove_entry_and_descendants` in a loop, which triggers `update_stat_for_remove_tx`, directly writing to `self.total_tx_size`/`self.total_tx_cycles` [3](#0-2) 
- `update_stat_for_remove_tx` mutates `self.total_tx_size` and `self.total_tx_cycles` in place [4](#0-3) 
- Lines 218–219 unconditionally overwrite those fields with the stale pre-eviction snapshot [5](#0-4) 
- `limit_size` blindly trusts `total_tx_size` as its sole eviction trigger [6](#0-5) 

---

Audit Report

## Title
`PoolMap::add_entry` Overwrites Eviction Decrements, Permanently Inflating `total_tx_size`/`total_tx_cycles` - (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, `updated_stat_for_add_tx` captures a pre-mutation snapshot of `total_tx_size` and `total_tx_cycles` into local variables before `check_and_record_ancestors` runs. When `check_and_record_ancestors` evicts entries via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, those decrements are applied directly to `self.total_tx_size`/`self.total_tx_cycles`. Lines 218–219 then unconditionally overwrite both fields with the stale pre-eviction snapshot, erasing all decrements. The counters are permanently inflated by the combined size/cycles of every evicted entry, causing `limit_size` to spuriously evict legitimate transactions and reject new ones.

## Finding Description

`add_entry` (L200–221) executes in this order:

```
L210-211: (total_tx_size, total_tx_cycles) = self.updated_stat_for_add_tx(entry.size, entry.cycles)?
           // snapshot: total_tx_size = self.total_tx_size + entry.size (immutable &self read)
L213:      evicts = self.check_and_record_ancestors(&mut entry)?
           // eviction branch (L603-625): calls remove_entry_and_descendants in a loop
           //   → remove_entry → update_stat_for_remove_tx
           //   → directly writes self.total_tx_size -= evicted_size (L738-740)
L218-219:  self.total_tx_size = total_tx_size   // stale snapshot written back
           self.total_tx_cycles = total_tx_cycles
```

`updated_stat_for_add_tx` (L711) is a `&self` method — it reads and returns new values without mutating. `update_stat_for_remove_tx` (L733) is `&mut self` and directly writes `self.total_tx_size` and `self.total_tx_cycles` (L738–740). The eviction branch in `check_and_record_ancestors` (L603–625) fires when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`, calling `remove_entry_and_descendants` in a loop. All decrements from those calls are silently overwritten at L218–219.

**Correct post-insertion value:** `original − Σ(evicted_sizes) + entry.size`
**Actual post-insertion value:** `original + entry.size`

No existing guard prevents this: `updated_stat_for_add_tx` only checks for overflow; `update_stat_for_remove_tx` has no awareness its writes will be overwritten; and `limit_size` (L298) blindly trusts `total_tx_size`.

## Impact Explanation

`total_tx_size` is the sole counter driving `limit_size` (L298: `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`). An inflated counter causes `limit_size` to evict legitimate pending/proposed transactions that would otherwise fit within the configured limit. Each subsequent eviction-during-insertion event adds more phantom bytes, so the discrepancy grows monotonically until node restart. A motivated attacker can repeatedly trigger this path to progressively hollow out the mempool, causing the node to reject valid transactions with `Reject::Full` and evict fee-paying transactions. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation

The eviction branch requires a transaction whose ancestor count exceeds `max_ancestors_count` (default 1,000) but whose `cell_ref_parents` subset is large enough to bring the count within the limit when removed. An attacker must pre-populate the pool with a chain of ~1,000 transactions where at least one ancestor is also a cell-dep of another ancestor. This is reachable via the `send_transaction` RPC or P2P relay without any privileged access, key material, or majority hash power. The cost is non-trivial (transaction fees for ~1,000 txs) but the bug can be triggered repeatedly to compound the drift, making the cost-to-impact ratio favorable for a motivated attacker. Likelihood is low-to-medium; impact per trigger is permanent until restart.

## Recommendation

Replace the pre-computed stale assignment at L218–219 with an incremental update applied after all mutations complete:

```rust
// Remove lines 218-219 and replace with:
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

This preserves all decrements applied by `update_stat_for_remove_tx` during eviction and adds only the new entry's contribution. Alternatively, remove `updated_stat_for_add_tx` from `add_entry` entirely and call a mutating `update_stat_for_add_tx` only after all mutations succeed, or call `recompute_total_stat()` whenever `add_entry` returns a non-empty `evicts` set.

## Proof of Concept

1. Configure a node with a small `max_ancestors_count` (e.g., 10 for a fast test).
2. Submit transactions `T1 → T2 → … → T_{N-1}` where `T1` is also referenced as a cell-dep by `T2` (making `T1` a `cell_ref_parent`). All are accepted; pool has `N-1` entries.
3. Record `get_tx_pool_info.total_tx_size` = `S_before`.
4. Submit `T_N` spending `T_{N-1}`'s output. Ancestor count = `N = max_ancestors_count + 1`, triggering the eviction branch. `T1` (and any descendants) are evicted; let their combined size be `S_evicted`.
5. After insertion, `get_tx_pool_info.total_tx_size` should equal `S_before - S_evicted + size(T_N)`.
6. **Observed:** `total_tx_size = S_before + size(T_N)` — inflated by `S_evicted`.
7. Repeat steps 2–6 to compound the drift. After `k` repetitions, `total_tx_size` exceeds the true pool size by `k × S_evicted`, causing `limit_size` to evict `k × S_evicted` worth of legitimate transactions on the next insertion.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-211)
```rust
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
```

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L616-625)
```rust
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

**File:** tx-pool/src/component/pool_map.rs (L738-740)
```rust
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```
