The code confirms the vulnerability. All claims in the report are verified against the actual source:

- `updated_stat_for_add_tx` takes `&self` and is read-only [1](#0-0) 
- `check_and_record_ancestors` calls `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which mutates `self.total_tx_size` [2](#0-1) 
- Lines 218–219 unconditionally overwrite the correctly-updated fields with the stale pre-eviction snapshot [3](#0-2) 

---

Audit Report

## Title
Stale Pre-Eviction Snapshot Overwrites Correctly Updated `total_tx_size`/`total_tx_cycles` After Ancestor-Count Eviction — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, `updated_stat_for_add_tx` computes a pre-eviction snapshot of `total_tx_size`/`total_tx_cycles` before `check_and_record_ancestors` runs. When `check_and_record_ancestors` evicts transactions to satisfy the ancestor-count limit, those evictions correctly decrement `self.total_tx_size`/`self.total_tx_cycles` via `update_stat_for_remove_tx`. However, `add_entry` then unconditionally overwrites those correctly-updated fields with the stale pre-eviction snapshot at lines 218–219, permanently inflating the pool's reported size and cycles by the total weight of every evicted transaction. This causes `limit_size` to spuriously evict valid transactions and reject new submissions with `Reject::Full`.

## Finding Description

The exact sequence in `add_entry` (lines 200–221):

```rust
// Step 1: READ-ONLY snapshot — does NOT mutate self
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
// total_tx_size = self.total_tx_size + entry.size (pre-eviction)

// Step 2: may evict N transactions via remove_entry_and_descendants
//   → remove_entry → update_stat_for_remove_tx
//   → self.total_tx_size -= evicted_size  (correctly mutates self)
evicts = self.check_and_record_ancestors(&mut entry)?;

// Step 3: OVERWRITES correctly-updated self.total_tx_size with stale snapshot
self.total_tx_size = total_tx_size;    // evictions erased
self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` (lines 711–729) is confirmed read-only — it takes `&self` and returns computed values without mutating any field. `update_stat_for_remove_tx` (lines 733–758) directly mutates `self.total_tx_size` and `self.total_tx_cycles`. The eviction path in `check_and_record_ancestors` (lines 603–625) is reached when `ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count`, calling `remove_entry_and_descendants` (line 618) → `remove_entry` (lines 235–250) → `update_stat_for_remove_tx` (line 247). All mutations to `self.total_tx_size` from evictions are then silently discarded by the overwrite at lines 218–219.

Correct post-operation value: `(old_total − evicted_sizes) + new_entry_size`
Actual stored value: `old_total + new_entry_size` (evicted sizes never subtracted)

## Impact Explanation

`total_tx_size` is the sole guard in `limit_size`: `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`. Because `total_tx_size` is permanently over-reported by the cumulative size of all ancestor-evicted transactions, `limit_size` evicts additional legitimate transactions that are not actually over the configured limit. Each subsequent call to `add_entry` that triggers the ancestor-eviction path compounds the inflation. The result is that valid pending transactions are spuriously dropped and new submissions receive `Reject::Full` even when real pool occupancy is well below the limit. An attacker can deliberately and repeatedly trigger this path to render a node's mempool effectively unusable for legitimate users, constituting **CKB network congestion with few costs** — matching the **High** impact class (10001–15000 points).

## Likelihood Explanation

The eviction path is reachable by any unprivileged user via `send_raw_transaction` RPC or P2P transaction relay. The attacker needs only to submit a transaction whose inputs or cell-deps reference outputs of existing pool transactions such that `ancestors_count > max_ancestors_count` (default 25) while `cell_ref_parents` is non-empty, satisfying the condition at line 603. This is a normal pattern for chained transactions sharing a cell dep. No key material, no special privilege, and no majority hash power is required. Since transactions are relayed P2P, all nodes processing the transaction are affected simultaneously.

## Recommendation

Move the stat assignment **after** `check_and_record_ancestors` completes, computing final totals from the already-updated `self.total_tx_size`/`self.total_tx_cycles`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Apply overflow-checked addition AFTER evictions have updated self fields
self.total_tx_size = self.total_tx_size
    .checked_add(entry.size)
    .ok_or_else(|| Reject::Full(...))?;
self.total_tx_cycles = self.total_tx_cycles
    .checked_add(entry.cycles)
    .ok_or_else(|| Reject::Full(...))?;
```

The overflow check previously performed in `updated_stat_for_add_tx` must be preserved and re-applied at this later point using the post-eviction `self.total_tx_size`.

## Proof of Concept

**Setup:** Pool with `max_ancestors_count = 25` and `max_tx_pool_size = 10_000` bytes. Pool holds 24 transactions forming a chain (ancestors of a 25th), plus one transaction `C` that is a `cell_ref_parent` of the incoming transaction `T`.

**Steps:**

1. Submit transaction `T` via `send_raw_transaction`. `T` has 25 ancestors (exceeds limit) but `cell_ref_parents = {C}`, so `ancestors_count − cell_ref_parents.len() = 24 ≤ max_ancestors_count`.
2. `add_entry` is called:
   - `updated_stat_for_add_tx` snapshots `total_tx_size_snapshot = self.total_tx_size + size(T)`.
   - `check_and_record_ancestors` evicts `C` (and its descendants). `remove_entry` → `update_stat_for_remove_tx` correctly sets `self.total_tx_size -= size(C)`.
   - Lines 218–219 write `self.total_tx_size = total_tx_size_snapshot`, restoring the pre-eviction value. `size(C)` is never subtracted.
3. **Result:** `self.total_tx_size` is now `size(C)` bytes higher than the actual sum of entries in the pool.
4. Repeat with additional transactions triggering the same path. Each iteration inflates `total_tx_size` further.
5. Eventually `total_tx_size > max_tx_pool_size` even though actual pool bytes are well below the limit. `limit_size` begins evicting valid transactions; new submissions receive `Reject::Full`.

A unit test can assert the invariant `pool_map.total_tx_size == pool_map.entries.iter().map(|e| e.size).sum()` before and after `add_entry` calls that trigger the ancestor-eviction path, which will fail on the current code and pass after the fix.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-219)
```rust
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
        trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
        evicts = self.check_and_record_ancestors(&mut entry)?;
        self.record_entry_edges(&entry)?;
        self.insert_entry(&entry, status);
        self.record_entry_descendants(&entry);
        self.track_entry_statics(None, Some(status));
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L244-247)
```rust
            self.remove_entry_edges(&entry.inner);
            self.remove_entry_links(id);
            self.track_entry_statics(Some(entry.status), None);
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
