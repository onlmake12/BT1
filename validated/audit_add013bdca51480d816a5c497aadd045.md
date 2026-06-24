The code is confirmed. The bug is real and the claim is accurate.

**Verification summary:**

- `updated_stat_for_add_tx` (lines 711–729) is `&self` — read-only, returns a snapshot `(self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles)` stored in local variables. [1](#0-0) 

- `check_and_record_ancestors` (line 213) calls `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which **mutates** `self.total_tx_size` and `self.total_tx_cycles` in place. [2](#0-1) 

- Lines 218–219 then unconditionally overwrite those correctly-decremented fields with the stale pre-eviction snapshot, permanently inflating `total_tx_size` by the cumulative size of all ancestor-evicted transactions. [3](#0-2) 

- `limit_size` uses `self.pool_map.total_tx_size > self.config.max_tx_pool_size` as its sole guard, so inflation directly causes spurious evictions and `Reject::Full`. [4](#0-3) 

---

Audit Report

## Title
Stale Pre-Eviction Snapshot Overwrites Correctly Updated `total_tx_size`/`total_tx_cycles` After Ancestor-Count Eviction — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, `updated_stat_for_add_tx` computes a candidate `(total_tx_size, total_tx_cycles)` snapshot from the current pool state before any eviction occurs. When `check_and_record_ancestors` subsequently evicts transactions via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, those evictions correctly decrement `self.total_tx_size` and `self.total_tx_cycles` in place. Lines 218–219 then unconditionally overwrite those correctly-updated fields with the stale pre-eviction snapshot, permanently inflating the reported pool size by the total weight of every ancestor-evicted transaction. This causes `limit_size` to spuriously evict legitimate transactions and reject new submissions with `Reject::Full` even when actual pool occupancy is below the configured limit.

## Finding Description

The exact sequence in `add_entry` (lines 200–221):

```rust
// Lines 210-211: read-only snapshot; self.total_tx_size is NOT mutated
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
// = (self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles)

// Line 213: may evict N transactions; each calls
//   remove_entry → update_stat_for_remove_tx → self.total_tx_size -= evicted_size
evicts = self.check_and_record_ancestors(&mut entry)?;

// Lines 218-219: OVERWRITES the correctly-updated self.total_tx_size/cycles
//   with the stale pre-eviction snapshot
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` (lines 711–729) takes `&self` and only computes candidate values without mutating `self`. `update_stat_for_remove_tx` (lines 733–757) mutates `self.total_tx_size` and `self.total_tx_cycles` in place via checked subtraction. The eviction path inside `check_and_record_ancestors` (lines 603–625) is reached when `ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count`, and calls `remove_entry_and_descendants` for each evicted candidate. `remove_entry` (lines 235–250) calls `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size`. Those decrements are then discarded when lines 218–219 restore the stale snapshot.

The correct post-operation value should be:
```
total_tx_size = (old_total - sum(evicted_sizes)) + new_entry_size
```
The actual stored value is:
```
total_tx_size = old_total + new_entry_size   // evicted_sizes never subtracted
```

## Impact Explanation

`limit_size` in `tx-pool/src/pool.rs` (line 298) uses `self.pool_map.total_tx_size > self.config.max_tx_pool_size` as the sole guard for pool-size eviction. Because `total_tx_size` is over-reported by the cumulative size of all ancestor-evicted transactions, `limit_size` will evict additional legitimate transactions that are not actually over the configured limit. Each subsequent call to `add_entry` that triggers the ancestor-eviction path compounds the inflation. An attacker can drive `total_tx_size` arbitrarily above the real pool occupancy, causing the node to spuriously evict valid pending transactions via `limit_size` and reject all new submissions with `Reject::Full` even when real pool occupancy is well below the limit. This constitutes a **High** impact: **Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, since an unprivileged attacker can render a node's mempool effectively non-functional for legitimate users, preventing transaction propagation and causing network-wide congestion at negligible cost.

## Likelihood Explanation

The eviction path is reachable by any unprivileged user via `send_raw_transaction` RPC or P2P relay. The attacker needs only to submit a transaction whose ancestor count exceeds `max_ancestors_count` (default 25) while `cell_ref_parents` is non-empty, so that `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. This is a normal pattern for chained transactions sharing a cell dep. No key material, hash power, or special privilege is required. The condition is reachable in ordinary mainnet operation and is repeatable: each qualifying submission inflates `total_tx_size` further.

## Recommendation

Move the stat assignment **after** `check_and_record_ancestors` completes, computing final totals from the already-updated `self.total_tx_size`/`self.total_tx_cycles` rather than from the pre-eviction snapshot:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Apply overflow-checked addition AFTER evictions have already updated self fields
self.total_tx_size = self.total_tx_size
    .checked_add(entry.size)
    .ok_or_else(|| Reject::Full(format!(
        "tx-pool total_tx_size {} overflows by add {}",
        self.total_tx_size, entry.size
    )))?;
self.total_tx_cycles = self.total_tx_cycles
    .checked_add(entry.cycles)
    .ok_or_else(|| Reject::Full(format!(
        "tx-pool total_tx_cycles {} overflows by add {}",
        self.total_tx_cycles, entry.cycles
    )))?;
```

The overflow check previously performed in `updated_stat_for_add_tx` must be preserved and re-applied at this later point using the post-eviction `self.total_tx_size`.

## Proof of Concept

**Setup:** Pool with `max_ancestors_count = 25`, `max_tx_pool_size = 10_000` bytes. Pool holds 24 transactions forming a chain (ancestors of a 25th), plus one transaction `C` that is a `cell_ref_parent` of the incoming transaction `T`.

**Steps:**

1. Submit `T` via `send_raw_transaction`. `T` has 25 ancestors but `cell_ref_parents = {C}`, so `ancestors_count - cell_ref_parents.len() = 24 <= 25`.
2. `add_entry` is called:
   - `updated_stat_for_add_tx` snapshots `total_tx_size_snapshot = self.total_tx_size + size(T)`.
   - `check_and_record_ancestors` evicts `C` (and its descendants). `remove_entry` → `update_stat_for_remove_tx` correctly sets `self.total_tx_size -= size(C)`.
   - Lines 218–219 write `self.total_tx_size = total_tx_size_snapshot`, restoring the pre-eviction value. `size(C)` is never subtracted.
3. **Result:** `self.total_tx_size` is now `size(C)` bytes higher than the actual sum of entries in the pool.
4. Repeat with additional transactions triggering the same path. Each iteration inflates `total_tx_size` by the size of the evicted entry.
5. Eventually `total_tx_size > max_tx_pool_size` even though actual pool bytes are well below the limit. `limit_size` begins evicting valid transactions; new submissions receive `Reject::Full`.

A unit test can confirm this by asserting `pool_map.total_tx_size == pool_map.recompute_total_stat().unwrap().0` after each `add_entry` call that triggers the ancestor-eviction path.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
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

**File:** tx-pool/src/component/pool_map.rs (L733-741)
```rust
    fn update_stat_for_remove_tx(&mut self, tx_size: usize, cycles: Cycle) {
        match (
            self.total_tx_size.checked_sub(tx_size),
            self.total_tx_cycles.checked_sub(cycles),
        ) {
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
            }
```

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```
