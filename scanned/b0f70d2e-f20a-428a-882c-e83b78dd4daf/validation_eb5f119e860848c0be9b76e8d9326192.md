The code confirms the bug exactly as described. Let me verify the full call chain once more.

Audit Report

## Title
`PoolMap::add_entry` Overwrites Eviction Decrements to `total_tx_size`/`total_tx_cycles` with Stale Pre-Computed Values - (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, `total_tx_size` and `total_tx_cycles` are snapshotted into local variables before `check_and_record_ancestors` runs. When that function evicts entries via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, it correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in place. However, `add_entry` then unconditionally overwrites those fields with the stale pre-eviction snapshot, permanently inflating both counters by the combined size/cycles of all evicted entries. Because `limit_size` uses `total_tx_size` as the sole gate for pool-capacity enforcement, the pool will spuriously evict and reject legitimate transactions.

## Finding Description

In `tx-pool/src/component/pool_map.rs` lines 200–221, `add_entry` executes the following sequence:

```
L210-211  local (total_tx_size, total_tx_cycles) = self.updated_stat_for_add_tx(...)
           // snapshot: self.total_tx_size + entry.size (no mutation yet)

L213      evicts = self.check_and_record_ancestors(&mut entry)?
           // may call remove_entry_and_descendants → remove_entry → update_stat_for_remove_tx
           // update_stat_for_remove_tx DIRECTLY writes self.total_tx_size -= evicted_size

L214      self.record_entry_edges(&entry)?   // may early-return (no stat write)
L215      self.insert_entry(...)
L218-219  self.total_tx_size  = total_tx_size   // ← stale snapshot, overwrites L213 decrements
          self.total_tx_cycles = total_tx_cycles // ← same
```

`updated_stat_for_add_tx` (lines 711–729) is a pure read that returns `self.total_tx_size + entry.size` without touching `self`. `update_stat_for_remove_tx` (lines 733–758) directly mutates `self.total_tx_size` and `self.total_tx_cycles`. The final write at lines 218–219 discards every decrement performed during eviction.

The eviction path in `check_and_record_ancestors` (lines 603–625) fires when:
- `ancestors_count > max_ancestors_count`, **and**
- `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`

Under those conditions, `remove_entry_and_descendants` is called for each `cell_ref_parent` candidate (line 618), which chains through `remove_entry` (line 263) → `update_stat_for_remove_tx` (line 247). Each call correctly decrements `self.total_tx_size`, but all decrements are erased by lines 218–219.

**Correct post-insertion value:** `original_total − Σ(evicted_sizes) + entry.size`
**Actual post-insertion value:** `original_total + entry.size`

The discrepancy equals `Σ(evicted_sizes)` and is permanent until node restart. Each subsequent eviction-during-insertion event adds more phantom bytes, so the drift grows monotonically.

No existing guard corrects this: `recompute_total_stat` (lines 698–708) is only invoked in the underflow branch of `update_stat_for_remove_tx`, never proactively after `add_entry`.

## Impact Explanation

`total_tx_size` is the sole counter checked by `limit_size` (pool.rs line 298: `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`). An inflated counter causes:

1. **Spurious evictions** — `limit_size` evicts legitimate pending/proposed transactions that would fit within the true pool size, permanently degrading mempool throughput.
2. **False `Reject::Full` responses** — new transactions are rejected even though actual occupancy is below the configured limit.
3. **Compounding drift** — each triggered eviction-during-insertion adds more phantom bytes; the pool's effective capacity shrinks monotonically.

This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** An attacker can repeatedly trigger this path to progressively shrink the effective mempool, causing sustained transaction rejection and relay degradation across the network.

## Likelihood Explanation

The eviction branch requires a transaction whose ancestor count exceeds `max_ancestors_count` (default 1 000) but where removing `cell_ref_parents` brings it back within the limit. An unprivileged attacker can craft this deliberately:

- Build a chain `T1 → T2 → … → T_{N-1}` where `T1` is also a cell-dep of `T2` (making it a `cell_ref_parent`), with `N-1 = max_ancestors_count`.
- Submit `T_N` spending `T_{N-1}`'s output; ancestor count becomes `N`, triggering eviction of `T1` and its descendants.
- No privileged access, key material, or majority hash power is required — only the ability to submit transactions via `send_transaction` RPC or P2P relay.
- The attack is repeatable: each round inflates `total_tx_size` by `Σ(evicted_sizes)`. The cost per round is the fees for ~1 000 transactions, but the damage compounds indefinitely.

Likelihood is **Low** per round due to the chain-length prerequisite, but the compounding nature means a single patient attacker can permanently degrade a target node's mempool.

## Recommendation

Replace the pre-computed stale assignment with an incremental update applied **after** all mutations complete. Since `update_stat_for_remove_tx` has already decremented `self.total_tx_size` for every evicted entry, only the new entry's contribution needs to be added:

```rust
// Remove lines 218-219 (stale snapshot write) and replace with:
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, move the `updated_stat_for_add_tx` call to after `check_and_record_ancestors` completes (so the snapshot is taken from the post-eviction state), or call `recompute_total_stat()` unconditionally at the end of `add_entry` when `!evicts.is_empty()`.

## Proof of Concept

1. Configure a node with `max_ancestors_count = N` (e.g., default 1 000).
2. Submit transactions `T1 → T2 → … → T_{N-1}` where `T1` is also referenced as a cell-dep by `T2`.
3. Record `get_tx_pool_info` → `total_tx_size = S_before`.
4. Submit `T_N` spending `T_{N-1}`'s output. Ancestor count = `N`, triggering eviction of `T1` (and descendants) inside `check_and_record_ancestors`.
5. `update_stat_for_remove_tx` decrements `self.total_tx_size` by `S_evicted` during eviction.
6. `add_entry` lines 218–219 overwrite with the stale snapshot, restoring `self.total_tx_size` to `S_before + size(T_N)`.
7. Query `get_tx_pool_info` → `total_tx_size` is `S_evicted` bytes higher than the true sum of entries in the pool.
8. Repeat steps 2–7 to compound the drift. After `K` rounds, `total_tx_size` exceeds the true value by `K × S_evicted`, causing `limit_size` to evict legitimate transactions and reject new ones even though the pool is well below its actual capacity.