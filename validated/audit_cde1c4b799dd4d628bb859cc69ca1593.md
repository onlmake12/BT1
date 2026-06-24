The code confirms the claim exactly. Key verification:

- `updated_stat_for_add_tx` takes `&self` (immutable) and **returns** computed values without mutating `self` [1](#0-0) 
- Lines 210–211 capture a pre-eviction snapshot into local variables [2](#0-1) 
- Line 213 calls `check_and_record_ancestors`, which at line 618 calls `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, **mutating** `self.total_tx_size` and `self.total_tx_cycles` [3](#0-2) 
- Lines 218–219 then unconditionally overwrite the correctly-decremented live counters with the stale snapshot [4](#0-3) 
- `limit_size` uses `total_tx_size` as its sole guard [5](#0-4) 

---

Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Permanently Inflated by Stale Pre-Eviction Snapshot Overwrite in `add_entry` - (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` computes and returns a pre-eviction snapshot of `total_tx_size` and `total_tx_cycles` without mutating `self`. When `check_and_record_ancestors` subsequently evicts cell-dep-conflicting transactions via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, the live counters are correctly decremented. Lines 218–219 then unconditionally overwrite those correctly-decremented live counters with the stale pre-eviction snapshot, permanently inflating both counters by the sum of all evicted entries' sizes and cycles. The inflation is cumulative and causes `limit_size` to cascade-evict legitimate transactions, constituting a remote denial-of-service against the tx-pool.

## Finding Description
`updated_stat_for_add_tx` (lines 711–729) takes `&self` (immutable reference) and returns `(self.total_tx_size + tx_size, self.total_tx_cycles + cycles)` without mutating any field. In `add_entry`:

- **Lines 210–211**: `let (total_tx_size, total_tx_cycles) = self.updated_stat_for_add_tx(entry.size, entry.cycles)?;` — captures a snapshot at pre-eviction state.
- **Line 213**: `evicts = self.check_and_record_ancestors(&mut entry)?;` — when `ancestors_count > max_ancestors_count` and `cell_ref_parents` are present (lines 603–625), this calls `remove_entry_and_descendants` (line 618), which chains to `remove_entry` (line 247), which calls `update_stat_for_remove_tx` (line 247), which correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in-place.
- **Lines 218–219**: `self.total_tx_size = total_tx_size; self.total_tx_cycles = total_tx_cycles;` — blindly restores the pre-eviction snapshot, erasing every decrement performed by `update_stat_for_remove_tx`.

Correct final value: `(original_total − Σ evicted_sizes) + new_entry_size`  
Actual stored value: `original_total + new_entry_size`

The underflow fallback in `update_stat_for_remove_tx` (lines 742–756) only triggers on arithmetic underflow, not on the subsequent overwrite at lines 218–219, which always succeeds silently.

## Impact Explanation
`total_tx_size` is the sole guard in `limit_size` (`pool.rs` line 298): `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`. Inflated `total_tx_size` causes `limit_size` to believe the pool is over-capacity when it is not, triggering cascading evictions of legitimate fee-paying transactions and returning `Reject::Full` to subsequent `send_transaction` RPC callers. The drift is permanent and cumulative — every insertion that triggers at least one cell-dep eviction adds phantom bytes. This matches the **High** impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation
The trigger condition — a new transaction whose cell dep is already consumed by existing pool transactions — is a normal operational scenario explicitly handled by the `cell_ref_parents` eviction path (lines 603–625). No privileged access, key material, or majority hashpower is required. An attacker only needs to observe cell deps referenced by pending pool transactions via `get_raw_tx_pool`, then submit transactions consuming those same cell deps as inputs, forcing eviction of existing transactions. Repeating this accumulates phantom inflation until `total_tx_size` permanently exceeds `max_tx_pool_size`. Any public RPC endpoint is exposed.

## Recommendation
Remove the pre-eviction snapshot pattern entirely. `updated_stat_for_add_tx` should be restructured to perform only the overflow pre-check, and the actual counter increment should be applied after `check_and_record_ancestors` completes, directly to the already-correct live counters:

```rust
pub(crate) fn add_entry(
    &mut self,
    mut entry: TxEntry,
    status: Status,
) -> Result<(bool, HashSet<TxEntry>), Reject> {
    let tx_short_id = entry.proposal_short_id();
    let mut evicts = Default::default();
    if self.entries.get_by_id(&tx_short_id).is_some() {
        return Ok((false, evicts));
    }
    // Overflow pre-check only — do not capture a snapshot
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    // Apply new entry's contribution AFTER evictions have already mutated the live counters
    self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
    self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
    Ok((true, evicts))
}
```

## Proof of Concept
**Setup:** Pool with `max_tx_pool_size = 10_000`, `max_ancestors_count = 3`. Pool contains `T_ref` (size 1000) and `T_dep` (size 1000) where `T_dep` uses `T_ref`'s output as a cell dep. `total_tx_size = 2000`.

**Step 1 — Attacker submits `T_new`** (size 500) consuming `T_ref`'s output as an input, making `T_dep` a `cell_ref_parent` that must be evicted:
- `updated_stat_for_add_tx(500)` → snapshot = `2000 + 500 = 2500`
- `check_and_record_ancestors` evicts `T_dep` (size 1000): `update_stat_for_remove_tx` sets `self.total_tx_size = 1000`
- Lines 218–219 overwrite: `self.total_tx_size = 2500` ← **wrong**
- Correct value: `1000 (T_ref) + 500 (T_new) = 1500`

**Step 2 — Repeat** with additional eviction-triggering transactions. Each iteration adds `evicted_size` phantom bytes. After enough iterations, `total_tx_size > max_tx_pool_size` permanently, and `limit_size` evicts every new submission with `Reject::Full`.

A unit test can be written directly against `PoolMap::add_entry` by constructing a pool with known entries, submitting a transaction that triggers the `cell_ref_parents` eviction path, and asserting `pool_map.total_tx_size == actual_sum_of_entry_sizes` after the call.

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

**File:** tx-pool/src/component/pool_map.rs (L615-625)
```rust
            let mut iter = evict_candidates.iter();
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

**File:** tx-pool/src/pool.rs (L297-299)
```rust
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
```
