Audit Report

## Title
Stale Pre-Eviction Totals Overwrite Eviction-Adjusted `total_tx_size`/`total_tx_cycles` in `PoolMap::add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` snapshots pre-eviction totals into local variables before `check_and_record_ancestors` runs. When that function evicts transactions, `update_stat_for_remove_tx` correctly decrements `self.total_tx_size`/`self.total_tx_cycles` in-place. However, lines 218–219 unconditionally overwrite those fields with the stale pre-eviction locals, permanently overstating both counters by the aggregate size and cycles of all evicted transactions. This causes the pool to believe it is fuller than it is, triggering spurious eviction of legitimate transactions and rejection of new submissions.

## Finding Description
The exact sequence in `add_entry` (lines 200–221) is confirmed by the code:

**Lines 210–211**: `updated_stat_for_add_tx(entry.size, entry.cycles)` reads `self.total_tx_size` and `self.total_tx_cycles`, adds the new entry's contribution, and returns the results as local variables. `self` is not mutated here. [1](#0-0) 

**Line 213**: `check_and_record_ancestors(&mut entry)` is called. When `ancestors_count > max_ancestors_count` but `ancestors_count.saturating_sub(cell_ref_parents.len()) <= max_ancestors_count` (lines 603–625), it calls `remove_entry_and_descendants`, which internally calls `update_stat_for_remove_tx`, which **directly mutates** `self.total_tx_size` and `self.total_tx_cycles`. [2](#0-1) [3](#0-2) 

**Lines 218–219**: `self.total_tx_size = total_tx_size; self.total_tx_cycles = total_tx_cycles;` — the stale pre-eviction locals are written back unconditionally, silently discarding all eviction-driven decrements. [4](#0-3) 

The `recompute_total_stat` fallback inside `update_stat_for_remove_tx` (lines 743–749) is only triggered on arithmetic underflow. Since the subtraction of a valid entry's size from a larger total does not underflow, this guard never fires on this path. [5](#0-4) 

## Impact Explanation
`total_tx_size` is the sole counter driving `limit_size` (pool.rs line 298), which loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting lowest-fee transactions. An overstated counter causes it to evict legitimate pending/proposed transactions that would otherwise fit. [6](#0-5) 

`updated_stat_for_add_tx` (lines 716–721) also rejects incoming transactions with `Reject::Full` on overflow. An overstated counter causes premature rejection of valid submissions. [7](#0-6) 

The result is a sustained, attacker-controlled DoS against the transaction pool: legitimate transactions are evicted and new ones are refused, preventing transaction propagation. This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
The eviction path in `check_and_record_ancestors` is reachable by any unprivileged peer via `send_transaction` RPC or P2P relay. The attacker must submit a chain of transactions where several share a cell dep (populating `cell_ref_parents`), then submit a new transaction whose ancestor count exceeds `max_ancestors_count` (default 25) but whose `cell_ref_parents` count is large enough to satisfy the eviction condition. No privileged access, majority hashpower, or victim mistakes are required. The overstatement accumulates with each such submission, making the attack repeatable and compounding.

## Recommendation
Move the accounting update to **after** `check_and_record_ancestors` completes, so eviction decrements are already reflected before the new entry's contribution is added:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Apply the new entry's contribution on top of already-eviction-adjusted self fields:
self.total_tx_size = self.total_tx_size.checked_add(entry.size)
    .ok_or_else(|| Reject::Full(...))?;
self.total_tx_cycles = self.total_tx_cycles.checked_add(entry.cycles)
    .ok_or_else(|| Reject::Full(...))?;
```

The overflow check currently in `updated_stat_for_add_tx` must be preserved at this new location.

## Proof of Concept
Concrete accounting trace:

| Step | Event | `self.total_tx_size` |
|---|---|---|
| Initial | Pool has 3 txs, total size 300 | 300 |
| `updated_stat_for_add_tx(50, …)` | Returns local `total_tx_size = 350`; `self` unchanged | 300 |
| `check_and_record_ancestors` evicts 2 txs (80 bytes each) | `update_stat_for_remove_tx` called twice | 300 − 80 − 80 = **140** |
| `self.total_tx_size = total_tx_size` (line 218) | Stale local written back | **350** (correct value: 190) |

Unit test plan: construct a `PoolMap` with `max_ancestors_count = 3`, insert 3 transactions sharing a cell dep (making them `cell_ref_parents`), insert a 4th transaction that has all 3 as ancestors. Assert that after `add_entry` returns, `pool_map.total_tx_size` equals the sum of only the surviving entries, not the pre-eviction sum.

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

**File:** tx-pool/src/component/pool_map.rs (L716-721)
```rust
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_size {} overflows by add {}",
                self.total_tx_size, tx_size
            ))
        })?;
```

**File:** tx-pool/src/component/pool_map.rs (L738-740)
```rust
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L742-749)
```rust
            _ => {
                if let Some((total_tx_size, total_tx_cycles)) = self.recompute_total_stat() {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, recomputed size {} cycles {}",
                        tx_size, cycles, total_tx_size, total_tx_cycles
                    );
                    self.total_tx_size = total_tx_size;
                    self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```
