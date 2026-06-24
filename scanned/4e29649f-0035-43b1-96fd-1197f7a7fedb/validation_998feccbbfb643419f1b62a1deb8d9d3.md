The code confirms the vulnerability. All three key behaviors are verified:

1. `add_entry` (L210-211, L218-219): pre-computes totals into locals, then unconditionally overwrites `self` fields after `check_and_record_ancestors` runs.
2. `check_and_record_ancestors` (L603-625): the eviction branch calls `remove_entry_and_descendants` → `remove_entry` (L247) → `update_stat_for_remove_tx`, which directly mutates `self.total_tx_size`/`self.total_tx_cycles`.
3. `updated_stat_for_add_tx` (L711): takes `&self`, returns new values without touching `self`.

The overwrite at L218-219 erases all decrements performed during eviction.

---

Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Inflated When `check_and_record_ancestors` Evicts Transactions During `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, new totals are pre-computed into local variables before `check_and_record_ancestors` runs. When that function evicts existing pool entries via `remove_entry_and_descendants` → `update_stat_for_remove_tx`, it correctly decrements `self.total_tx_size` and `self.total_tx_cycles`. However, `add_entry` then unconditionally overwrites those fields with the stale pre-eviction locals, permanently inflating both counters by the total size/cycles of every evicted transaction. An unprivileged submitter can repeatedly trigger this path to make the pool believe it is full, denying service to all subsequent legitimate transaction submissions on the targeted node.

## Finding Description
In `tx-pool/src/component/pool_map.rs`, `add_entry` executes:

```rust
// L210-211: compute new totals into LOCAL variables — self is NOT modified
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// L213: may evict entries, calling update_stat_for_remove_tx which DOES modify self
evicts = self.check_and_record_ancestors(&mut entry)?;

// L218-219: OVERWRITES the post-eviction self fields with the pre-eviction locals
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` (L711-729) takes `&self` and returns `(self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles)` without touching `self`.

Inside `check_and_record_ancestors` (L603-625), the eviction branch fires when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. It calls `remove_entry_and_descendants` for each evicted ancestor. That calls `remove_entry` (L247), which calls `update_stat_for_remove_tx` (L733-758), which subtracts the evicted entry's `size` and `cycles` directly from `self.total_tx_size` and `self.total_tx_cycles`.

After `check_and_record_ancestors` returns, the overwrite at L218-219 restores the pre-eviction values, erasing every decrement. Net effect per trigger:

```
self.total_tx_size  = old_total + new_entry_size          (actual written value)
correct value       = old_total − Σ(evicted_sizes) + new_entry_size
inflation           = Σ(evicted_sizes)
```

Each successful trigger permanently inflates the counters by the total size of the evicted transactions.

## Impact Explanation
`total_tx_size` is the sole guard in `limit_size` (pool.rs L298): `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`. An inflated counter causes `limit_size` to evict legitimate transactions that should remain in the pool, and causes `updated_stat_for_add_tx` to reject new submissions with `Reject::Full` even when the pool has ample real capacity. By repeatedly triggering the eviction path, an attacker can drive `total_tx_size` arbitrarily high relative to actual pool contents, making the tx-pool permanently reject all new transactions on the targeted node. This matches the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs* (10001–15000 points), as a targeted node becomes unable to relay or submit transactions.

## Likelihood Explanation
Any unprivileged actor who can submit transactions via the `send_transaction` RPC or P2P relay protocol can trigger this. The attacker needs only to submit a chain of ~25 transactions where at least one uses a shared cell as a `cell_dep` (making it a "cell-ref parent"), then submit a new transaction whose ancestor count exceeds `max_ancestors_count` (default 25) but whose cell-ref parents account for the excess. No privileged keys, majority hashpower, or social engineering are required. The attack is repeatable and cumulative — each iteration inflates the counters further.

## Recommendation
In `add_entry`, do not store the result of `updated_stat_for_add_tx` — use it only for overflow validation. After `check_and_record_ancestors` completes (so evictions are already reflected in `self`), apply the new entry's contribution directly:

```rust
pub(crate) fn add_entry(
    &mut self,
    mut entry: TxEntry,
    status: Status,
) -> Result<(bool, HashSet<TxEntry>), Reject> {
    let tx_short_id = entry.proposal_short_id();
    if self.entries.get_by_id(&tx_short_id).is_some() {
        return Ok((false, Default::default()));
    }
    // Validate overflow only — do NOT store the result
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

    let evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));

    // Apply new entry's contribution on top of post-eviction totals
    self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
    self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
    Ok((true, evicts))
}
```

## Proof of Concept
1. Submit 26 transactions: tx₁…tx₂₅ form a chain; tx₁ also uses a shared cell as `cell_dep` (making it a cell-ref parent).
2. Submit tx₂₆ that spends tx₂₅'s output — its ancestor count is 26 > `max_ancestors_count` (25), but `26 - 1 = 25 ≤ 25`, so the eviction branch fires.
3. `check_and_record_ancestors` evicts tx₁ (and its descendants) via `remove_entry_and_descendants`, decrementing `self.total_tx_size` by their sizes.
4. `add_entry` then overwrites `self.total_tx_size` with the pre-eviction local value, inflating it by the evicted sizes.
5. Repeat steps 1–4; observe `total_tx_size` growing beyond the actual pool contents.
6. Attempt to submit any new transaction; observe `Reject::Full` despite the pool being below `max_tx_pool_size` in reality.

A unit test can assert `pool_map.total_tx_size == pool_map.entries.iter().map(|e| e.size).sum()` after each insertion to detect the invariant violation directly.