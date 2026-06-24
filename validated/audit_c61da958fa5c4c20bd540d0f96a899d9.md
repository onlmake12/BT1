Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Permanently Inflated by Stale Pre-Eviction Snapshot Overwrite in `add_entry` - (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, aggregate pool counters (`total_tx_size`, `total_tx_cycles`) are snapshotted before `check_and_record_ancestors` runs. When that function evicts cell-dep-conflicting transactions via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, the live counters are correctly decremented. However, lines 218–219 then unconditionally overwrite the live counters with the stale pre-eviction snapshot, permanently inflating both by the sum of all evicted entries' sizes and cycles. The inflation is cumulative and causes `limit_size` to cascade-evict legitimate transactions, constituting a remote denial-of-service against the tx-pool.

## Finding Description
The exact sequence in `add_entry` (lines 200–221):

```
Line 210-211: snapshot = self.total_tx_size + entry.size   ← stale, pre-eviction
Line 213:     check_and_record_ancestors()
                → remove_entry_and_descendants()
                  → remove_entry()
                    → update_stat_for_remove_tx()           ← correctly decrements self.total_tx_size
Lines 218-219: self.total_tx_size = snapshot               ← OVERWRITES correct value
               self.total_tx_cycles = snapshot_cycles
```

`updated_stat_for_add_tx` (lines 711–729) reads `self.total_tx_size` at call time and adds `entry.size`; it has no knowledge of subsequent evictions. `check_and_record_ancestors` (lines 588–640) calls `remove_entry_and_descendants` (line 618) when `ancestors_count > max_ancestors_count` and `cell_ref_parents` are present, which chains to `remove_entry` (line 247) calling `update_stat_for_remove_tx` (lines 733–758) to decrement `self.total_tx_size`. After all evictions, lines 218–219 blindly restore the pre-eviction snapshot, erasing every decrement.

Correct final value: `(original_total − Σ evicted_sizes) + new_entry_size`
Actual stored value: `original_total + new_entry_size`

Existing guards are insufficient: `update_stat_for_remove_tx` has an underflow fallback that calls `recompute_total_stat`, but this path is only triggered on underflow — not on the overwrite at lines 218–219, which always succeeds silently.

## Impact Explanation
`total_tx_size` is the sole guard in `limit_size` (pool.rs line 298): `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`. Inflated `total_tx_size` causes `limit_size` to believe the pool is over-capacity when it is not, triggering cascading evictions of legitimate fee-paying transactions and returning `Reject::Full` to subsequent `send_transaction` RPC callers. The drift is permanent and cumulative — every insertion that triggers at least one cell-dep eviction adds phantom bytes. This matches the **High** impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation
The trigger condition — a new transaction whose cell dep is already consumed by existing pool transactions — is a normal operational scenario explicitly handled by the `cell_ref_parents` eviction path. No privileged access, key material, or majority hashpower is required. An attacker only needs to:
1. Observe cell deps referenced by pending pool transactions via `get_raw_tx_pool`.
2. Submit a transaction consuming those same cell deps as inputs, forcing eviction of existing transactions.
3. Repeat to accumulate phantom inflation until `total_tx_size` permanently exceeds `max_tx_pool_size`.

Any public RPC endpoint is exposed.

## Recommendation
Remove the pre-eviction snapshot pattern entirely. After `check_and_record_ancestors` completes, apply the new entry's contribution directly to the already-correct live counters:

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

A unit test can be written directly against `PoolMap::add_entry` by constructing a pool with known entries, submitting a transaction that triggers the `cell_ref_parents` eviction path, and asserting `pool_map.total_tx_size == actual_sum_of_entry_sizes` after the call. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** tx-pool/src/component/pool_map.rs (L244-248)
```rust
            self.remove_entry_edges(&entry.inner);
            self.remove_entry_links(id);
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
            entry.inner
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

**File:** tx-pool/src/pool.rs (L297-299)
```rust
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
```
