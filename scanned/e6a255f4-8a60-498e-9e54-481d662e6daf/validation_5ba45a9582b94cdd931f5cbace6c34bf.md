The code matches the claim exactly. All cited references are verified:

- Lines 210-211: snapshot taken before evictions [1](#0-0) 
- Line 213: `check_and_record_ancestors` triggers evictions [2](#0-1) 
- Lines 603-625: eviction path via `remove_entry_and_descendants` [3](#0-2) 
- Line 247: `update_stat_for_remove_tx` correctly decrements during eviction [4](#0-3) 
- Lines 218-219: stale snapshot unconditionally written back [5](#0-4) 
- Line 298: `limit_size` uses `total_tx_size` as sole eviction trigger [6](#0-5) 

---

Audit Report

## Title
Stale Pre-Eviction Snapshot Permanently Inflates `total_tx_size` in `PoolMap::add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` captures `self.total_tx_size + entry.size` into a local variable before `check_and_record_ancestors` runs. When `check_and_record_ancestors` evicts cell-dep parent transactions via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, those evictions correctly decrement `self.total_tx_size`. However, the stale pre-eviction snapshot is then unconditionally written back to `self.total_tx_size`, erasing the decrements and permanently inflating the counter by the aggregate size of all evicted transactions. This inflation causes `limit_size` to evict valid transactions and reject new submissions with `Reject::Full` even when actual pool occupancy is well below the configured limit.

## Finding Description
`add_entry` (lines 200–221) executes in this order:

1. **Line 210–211**: `updated_stat_for_add_tx(entry.size, entry.cycles)` reads `self.total_tx_size` and `self.total_tx_cycles`, adds the new entry's size/cycles, and stores the results in local variables `total_tx_size` and `total_tx_cycles`. At this point no evictions have occurred.

2. **Line 213**: `check_and_record_ancestors(&mut entry)` is called. When `ancestors_count > max_ancestors_count` but `ancestors_count.saturating_sub(cell_ref_parents.len()) <= max_ancestors_count` (lines 598, 603), the function enters the eviction branch (lines 615–625) and calls `remove_entry_and_descendants` for each cell-dep parent to be evicted. Each call chains to `remove_entry` (line 263), which calls `update_stat_for_remove_tx` (line 247), correctly decrementing `self.total_tx_size` and `self.total_tx_cycles` by the evicted transaction's size and cycles.

3. **Lines 218–219**: The stale local variables are written back unconditionally:
   ```rust
   self.total_tx_size = total_tx_size;   // pre-eviction value
   self.total_tx_cycles = total_tx_cycles;
   ```
   This overwrites the correctly-decremented live counters with the snapshot taken before any evictions, inflating `total_tx_size` by `Σ(evicted.size)` and `total_tx_cycles` by `Σ(evicted.cycles)`.

The `recompute_total_stat` self-healing path (lines 742–755) is only triggered on underflow, never on overflow, so the inflation is never corrected.

## Impact Explanation
`limit_size` (pool.rs line 298) uses `self.pool_map.total_tx_size > self.config.max_tx_pool_size` as its sole eviction condition. An inflated `total_tx_size` causes `limit_size` to believe the pool is over capacity and evict otherwise-valid pending transactions, and to reject new valid submissions with `Reject::Full`. The inflation accumulates across repeated invocations of the attack, compounding with each iteration. This constitutes a **High** impact: a vulnerability that can cause CKB network congestion (tx-pool DoS / censorship) with minimal cost to the attacker, matching the allowed impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)*.

## Likelihood Explanation
The trigger condition — a new transaction whose ancestor count exceeds `max_ancestors_count` due to cell-dep references, but falls within limits after evicting those cell-dep parents — is reachable by any unprivileged caller of the `send_transaction` RPC or any P2P relay peer. No key material, privileged access, or majority hash power is required. The attacker pre-populates the pool with a chain of transactions used as cell deps, then submits a crafted transaction that pushes the ancestor count just over the limit. The attack is repeatable and the inflation accumulates monotonically.

## Recommendation
Move the `updated_stat_for_add_tx` call to after `check_and_record_ancestors` completes, so evictions are already reflected in `self.total_tx_size` before the snapshot is taken:

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

This ensures the snapshot already incorporates any eviction decrements before being written back.

## Proof of Concept
1. Configure a node with `max_ancestors_count = N` and a finite `max_tx_pool_size`.
2. Submit transactions `T1 … TN` where each `Ti` is referenced as a cell dep by `T_{i+1}`, forming a chain of depth `N`. All are accepted into the pool; `total_tx_size` reflects their aggregate size `S`.
3. Submit a new transaction `Tnew` that references `T1` as a cell dep. Its ancestor count is `N+1 > max_ancestors_count`, but `cell_ref_parents = {T1}` so `(N+1) - 1 = N <= max_ancestors_count`. The eviction branch fires: `T1` (and its descendants) are removed via `remove_entry_and_descendants`. `self.total_tx_size` is decremented by `T1.size + descendants_sizes = D`. Then `self.total_tx_size = total_tx_size` (the pre-eviction snapshot `S + Tnew.size`) is written back, inflating the counter by `D`.
4. Observe via RPC or logs that `total_tx_size` is now `S + Tnew.size` instead of the correct `S - D + Tnew.size`.
5. Repeat steps 2–4. Each iteration inflates `total_tx_size` by `D`. After `ceil((max_tx_pool_size - S) / D)` iterations, `total_tx_size > max_tx_pool_size` even though the actual pool is nearly empty. `limit_size` begins evicting all remaining transactions and new submissions are rejected with `Reject::Full`.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-211)
```rust
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
```

**File:** tx-pool/src/component/pool_map.rs (L213-213)
```rust
        evicts = self.check_and_record_ancestors(&mut entry)?;
```

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L247-247)
```rust
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
```

**File:** tx-pool/src/component/pool_map.rs (L603-625)
```rust
        if ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count {
            // if ancestors count exceed limitation,
            // try to evict some conflicted transactions due to ref cells

            // sort them to find out the transactions with lowest fees
            let evict_candidates: Vec<ProposalShortId> = self
                .entries
                .iter_by_evict_key()
                .filter(move |entry| cell_ref_parents.contains(&entry.id))
                .map(|x| x.id.clone())
                .collect();

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

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```
