Audit Report

## Title
Stale Pre-Eviction Totals Overwrite Eviction-Adjusted `total_tx_size`/`total_tx_cycles` in `PoolMap::add_entry` — (`tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` computes new aggregate totals into local variables before `check_and_record_ancestors` runs. When that function evicts conflicting transactions, `update_stat_for_remove_tx` correctly decrements `self.total_tx_size`/`self.total_tx_cycles` in-place. However, the stale pre-eviction locals are then unconditionally written back at lines 218–219, silently discarding all eviction-driven decrements. This permanently overstates both counters by the aggregate size and cycles of every evicted transaction, causing the pool to believe it is fuller than it actually is and triggering spurious downstream evictions and rejections.

## Finding Description
The exact sequence in `add_entry` (lines 200–221):

1. **Lines 210–211** — `updated_stat_for_add_tx` reads `self.total_tx_size` and `self.total_tx_cycles`, adds the new entry's contribution, and stores the results in **local** variables `total_tx_size` / `total_tx_cycles`. `self.*` is not yet mutated. [1](#0-0) 

2. **Line 213** — `check_and_record_ancestors` is called. When `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (line 603), it enters the eviction branch and calls `remove_entry_and_descendants` for each candidate. [2](#0-1) 

3. `remove_entry_and_descendants` → `remove_entry` (line 235) → `update_stat_for_remove_tx` (line 247) directly mutates `self.total_tx_size` and `self.total_tx_cycles` via checked subtraction (lines 739–740). [3](#0-2) [4](#0-3) 

4. **Lines 218–219** — The stale pre-eviction locals are unconditionally assigned back to `self`, overwriting every decrement applied by step 3. [5](#0-4) 

No guard exists between steps 2 and 4 to reconcile the two mutation paths. The `update_stat_for_remove_tx` comment at line 731–733 even acknowledges existing accounting inaccuracy, confirming this is a known fragile area. [6](#0-5) 

## Impact Explanation
`total_tx_size` is the sole counter driving two critical admission gates:

- **`limit_size`** (pool.rs line 298) loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting legitimate pending/proposed transactions until the (overstated) counter falls below the limit. [7](#0-6) 

- **`updated_stat_for_add_tx`** (pool_map.rs line 716) rejects incoming transactions with `Reject::Full` when the (overstated) counter would overflow. [8](#0-7) 

Because the overstatement is permanent and cumulative (each eviction-during-insertion adds another delta), a sustained sequence of such insertions progressively inflates `total_tx_size` without bound relative to actual pool contents. The result is that the pool evicts legitimate transactions and rejects new submissions even though real capacity exists — a sustained, externally-triggerable DoS against the transaction pool. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
The eviction path at line 603 is reachable by any unprivileged peer via `send_transaction` RPC or P2P relay. The attacker must:

1. Submit a set of transactions that share a common cell dep, populating `cell_ref_parents` in the pool.
2. Submit a new transaction whose ancestor count exceeds `max_ancestors_count` (default 25) but whose `cell_ref_parents` overlap is large enough that `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`.

This is a realistic, low-cost sequence requiring no privileged access, no majority hashpower, and no victim mistakes. The effect is repeatable: each successful trigger permanently inflates the counters further.

## Recommendation
Move the accounting update to **after** `check_and_record_ancestors` returns, so eviction decrements are already reflected in `self.*` before the new entry's contribution is added:

```rust
// Remove pre-computation of total_tx_size/total_tx_cycles before check_and_record_ancestors.
// After check_and_record_ancestors returns (evictions already applied to self.*):
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Now add the new entry's contribution on top of the already-eviction-adjusted self.*:
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Or equivalently, replace the call to `updated_stat_for_add_tx` (which returns locals) with a call to `update_stat_for_add_tx` (a `&mut self` variant) placed after `check_and_record_ancestors`.

## Proof of Concept
Concrete accounting trace with initial pool size 300 bytes (3 txs × 100 bytes), new entry size 50 bytes, two evicted txs of 80 bytes each:

| Step | Event | `self.total_tx_size` |
|---|---|---|
| Initial | 3 txs in pool | 300 |
| `updated_stat_for_add_tx(50)` | Local `total_tx_size = 350`; `self.*` unchanged | 300 |
| `check_and_record_ancestors` evicts tx A (80 bytes) | `update_stat_for_remove_tx(80)` → `self.total_tx_size = 220` | 220 |
| evicts tx B (80 bytes) | `update_stat_for_remove_tx(80)` → `self.total_tx_size = 140` | 140 |
| `self.total_tx_size = total_tx_size` | Stale local written back | **350** (correct: 190) |

Overstatement: **160 bytes** (sum of evicted sizes). Repeating this sequence `k` times inflates the counter by `160k` bytes. A unit test asserting `pool_map.total_tx_size == pool_map.recompute_total_stat().0` after a triggered eviction-during-insertion will fail, directly reproducing the bug. The existing `recompute_total_stat` helper (lines 695–708) provides the ground-truth value for such an assertion. [9](#0-8)

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

**File:** tx-pool/src/component/pool_map.rs (L246-247)
```rust
            self.track_entry_statics(Some(entry.status), None);
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

**File:** tx-pool/src/component/pool_map.rs (L695-708)
```rust
        }
    }

    fn recompute_total_stat(&self) -> Option<(usize, Cycle)> {
        self.entries.iter().try_fold(
            (0usize, 0 as Cycle),
            |(total_size, total_cycles), (_, entry)| {
                Some((
                    total_size.checked_add(entry.inner.size)?,
                    total_cycles.checked_add(entry.inner.cycles)?,
                ))
            },
        )
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

**File:** tx-pool/src/component/pool_map.rs (L731-733)
```rust
    /// Update size and cycles statistics for remove tx
    /// cycles overflow is possible, currently obtaining cycles is not accurate
    fn update_stat_for_remove_tx(&mut self, tx_size: usize, cycles: Cycle) {
```

**File:** tx-pool/src/component/pool_map.rs (L738-740)
```rust
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/pool.rs (L298-307)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
```
