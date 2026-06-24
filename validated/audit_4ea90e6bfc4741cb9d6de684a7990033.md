Audit Report

## Title
Stale Pre-Eviction Snapshot Overwrites Correctly-Decremented `total_tx_size` in `PoolMap::add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, `updated_stat_for_add_tx` captures `self.total_tx_size + entry.size` into a local snapshot before `check_and_record_ancestors` runs. When `check_and_record_ancestors` evicts cell-dep parent transactions via `remove_entry_and_descendants`, each eviction correctly decrements `self.total_tx_size` through `update_stat_for_remove_tx`. However, the stale pre-eviction snapshot is then unconditionally written back to `self.total_tx_size`, permanently inflating it by the aggregate size of all evicted transactions. Repeated triggering accumulates unbounded inflation, eventually causing `limit_size` to evict all valid pool entries and reject new submissions with `Reject::Full`.

## Finding Description

The exact sequence in `add_entry` (L210–220):

```rust
// 1. Snapshot taken BEFORE evictions
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;  // L210-211

// 2. Evictions happen HERE — self.total_tx_size is decremented
evicts = self.check_and_record_ancestors(&mut entry)?;         // L213

// 3. Stale snapshot written back, overwriting the decremented value
self.total_tx_size = total_tx_size;                            // L218
self.total_tx_cycles = total_tx_cycles;                        // L219
```

`updated_stat_for_add_tx` (L716) reads `self.total_tx_size` at call time and returns `self.total_tx_size + entry.size` — before any evictions have occurred.

Inside `check_and_record_ancestors` (L603–625), when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`, the code evicts cell-dep parents via `remove_entry_and_descendants` (L618). Each call chains to `remove_entry` (L247), which calls `update_stat_for_remove_tx`, correctly decrementing `self.total_tx_size` by the evicted transaction's size.

After `check_and_record_ancestors` returns, the stale snapshot (which does not reflect those decrements) is written back unconditionally at L218–219. The net result per invocation:

| Counter | Correct value | Actual value written |
|---|---|---|
| `total_tx_size` | `original − Σ(evicted.size) + entry.size` | `original + entry.size` |
| `total_tx_cycles` | `original − Σ(evicted.cycles) + entry.cycles` | `original + entry.cycles` |

The self-healing path in `update_stat_for_remove_tx` (L742–756) only triggers on underflow (checked_sub failure), never on overflow, so the inflation is never corrected.

## Impact Explanation

`limit_size` (pool.rs L298) uses `self.pool_map.total_tx_size` as the sole counter to enforce `max_tx_pool_size`. An inflated counter causes `limit_size` to continuously evict otherwise-valid pending transactions and reject new submissions with `Reject::Full`, even when actual pool occupancy is well below the configured limit. Repeated triggering compounds the inflation without bound. This constitutes a **High** severity impact: **Vulnerabilities or bad designs which could cause CKB network congestion with few costs** (10001–15000 points). A single attacker node can render the tx-pool of any targeted full node permanently non-functional for transaction relay and submission, degrading network throughput.

## Likelihood Explanation

The trigger requires no privileged access, key material, or majority hash power. Any unprivileged `send_transaction` RPC caller or P2P relay peer can reach the eviction branch in `check_and_record_ancestors` by constructing transactions where some ancestors are cell-dep parents, pushing `ancestors_count` just above `max_ancestors_count` while keeping `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. The attack is repeatable: each crafted submission inflates `total_tx_size` further, and the inflation accumulates monotonically across invocations.

## Recommendation

Move the stat snapshot computation to after `check_and_record_ancestors` completes, so evictions are already reflected in `self.total_tx_size` when the snapshot is taken:

```diff
- let (total_tx_size, total_tx_cycles) =
-     self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
  trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
  evicts = self.check_and_record_ancestors(&mut entry)?;
  self.record_entry_edges(&entry)?;
  self.insert_entry(&entry, status);
  self.record_entry_descendants(&entry);
  self.track_entry_statics(None, Some(status));
- self.total_tx_size = total_tx_size;
- self.total_tx_cycles = total_tx_cycles;
+ let (total_tx_size, total_tx_cycles) =
+     self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
+ self.total_tx_size = total_tx_size;
+ self.total_tx_cycles = total_tx_cycles;
  Ok((true, evicts))
```

This ensures the snapshot is computed after evictions have already decremented `self.total_tx_size`, producing the correct post-eviction value.

## Proof of Concept

1. Configure a node with `max_ancestors_count = N` and a modest `max_tx_pool_size`.
2. Submit transactions `T1 … TN` where each `Ti` is referenced as a cell dep by `T_{i+1}`, forming a chain of depth `N`. All are accepted into the pool; `total_tx_size` reflects their aggregate size `S`.
3. Submit `Tnew` referencing `T1` as a cell dep. Its ancestor count is `N+1 > max_ancestors_count`, but `(N+1) - 1 = N <= max_ancestors_count`, so the eviction branch fires. `T1` (and its descendants) are evicted; `self.total_tx_size` is decremented correctly to `S - evicted_size + Tnew.size`. Then the stale snapshot `S + Tnew.size` is written back, inflating `total_tx_size` by `evicted_size`.
4. Repeat steps 2–3. Each iteration inflates `total_tx_size` by the size of the evicted chain.
5. After enough iterations, `total_tx_size > max_tx_pool_size` even though the pool contains few or no transactions. `limit_size` evicts all remaining entries; subsequent submissions return `Reject::Full`.
6. Verify by asserting `pool_map.total_tx_size` against the sum of `entry.size` for all entries actually present in `pool_map.entries` after each iteration — the two values diverge immediately on the first eviction-triggering submission.