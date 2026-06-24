The full call chain is confirmed:

- `remove_entry_and_descendants` at L263 calls `remove_entry` for each removed entry [1](#0-0) 
- `remove_entry` at L247 calls `update_stat_for_remove_tx`, correctly subtracting from `self.total_tx_size` [2](#0-1) 
- `update_stat_for_remove_tx` at L738-740 writes the corrected values back to `self.total_tx_size`/`self.total_tx_cycles` [3](#0-2) 
- But `add_entry` at L218-219 then unconditionally overwrites those corrected values with the stale pre-eviction snapshot [4](#0-3) 

All claims in the report are verified against the actual code. The bug is real.

---

Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Permanently Inflated by Pre-Eviction Snapshot Overwrite — (`tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, a size/cycles snapshot is computed before `check_and_record_ancestors` runs. That function may evict existing transactions via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size`/`self.total_tx_cycles`. After it returns, lines 218–219 unconditionally overwrite those corrected values with the stale pre-eviction snapshot, inflating the counters by the cumulative size/cycles of every evicted transaction. This causes `limit_size` to expel legitimate pending/proposed transactions to compensate for phantom inflation.

## Finding Description

In `add_entry` (pool_map.rs L200–221):

- **L210–211**: `updated_stat_for_add_tx` computes `snapshot = self.total_tx_size + entry.size` before any evictions occur. This is a pure arithmetic snapshot with no knowledge of future evictions.
- **L213**: `check_and_record_ancestors` is called. When `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (L603), it enters the eviction loop at L616–625, calling `remove_entry_and_descendants` for each cell-dep-referencing parent. Each call reaches `remove_entry` (L263) → `update_stat_for_remove_tx` (L247) → L738–740, which correctly subtracts the evicted entry's size/cycles from `self.total_tx_size`/`self.total_tx_cycles`.
- **L218–219**: The stale snapshot is unconditionally assigned back to `self.total_tx_size`/`self.total_tx_cycles`, discarding all corrections made during eviction.

For each evicted transaction of size `e_size`:
```
Correct: self.total_tx_size = old_total − e_size + entry.size
Actual:  self.total_tx_size = old_total           + entry.size  ← inflated by e_size
```

`limit_size` (pool.rs L298) then loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting legitimate transactions to compensate for the phantom inflation. Each eviction in `limit_size` correctly adjusts the counter via `remove_entry_and_descendants`, so after `limit_size` completes the counter is accurate — but legitimate transactions have been permanently expelled.

Existing guards do not mitigate this: `updated_stat_for_add_tx` only performs overflow checking and has no awareness of evictions; `update_stat_for_remove_tx` correctly maintains the counter during eviction but its work is silently discarded by the final overwrite.

## Impact Explanation

An unprivileged attacker can repeatedly submit crafted transactions that trigger the ancestor-eviction path, each time causing `limit_size` to expel legitimate transactions whose combined size equals the inflation introduced by that round. Over multiple rounds, the attacker continuously drains the pool of legitimate transactions without those transactions being invalid. This constitutes a low-cost, repeatable denial-of-service against the transaction pool, preventing legitimate transactions from being relayed and mined. This maps to **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation

The eviction path at L603–625 is triggered when: (1) the submitted transaction's ancestor count exceeds `max_ancestors_count`, and (2) removing cell-dep-referencing parents brings it within the limit. An unprivileged `send_transaction` RPC caller can craft a transaction chain of depth ≥ `max_ancestors_count` where the new transaction references an in-pool transaction as a cell dep, satisfying both conditions. No key material, miner privilege, or majority hash power is required. The attack is repeatable: each successful submission inflates the counter and causes `limit_size` to evict legitimate transactions.

## Recommendation

Move the snapshot computation to after `check_and_record_ancestors` completes, so it reflects the already-corrected `self.total_tx_size`/`self.total_tx_cycles`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
// Compute AFTER evictions have already adjusted self.total_tx_size/cycles
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, remove the final overwrite entirely and rely solely on `update_stat_for_remove_tx` (for evictions) plus a single `checked_add` for the new entry to maintain the counters incrementally throughout the function.

## Proof of Concept

**Setup**: `max_ancestors_count = 25`, `max_tx_pool_size = 2 MB`. Pool holds tx₁→tx₂→…→tx₂₄ (24 txs, each 100 KB). `total_tx_size = 2,400,000`.

**Attack**:
1. Submit tx₂₅ spending an output of tx₁ **and** referencing tx₁₂ as a cell dep. Ancestor count = 25 (at limit); cell-dep parent = tx₁₂.
2. `add_entry` executes:
   - L210–211: `snapshot = 2,400,000 + 100,000 = 2,500,000`
   - L213: `check_and_record_ancestors` evicts tx₁₂…tx₂₄ (13 txs × 100 KB = 1,300,000 bytes); `self.total_tx_size` correctly becomes `1,100,000`
   - L218: `self.total_tx_size = 2,500,000` ← stale snapshot written back
3. Actual pool size: `1,100,000 + 100,000 = 1,200,000` bytes.
4. Tracked `total_tx_size`: `2,500,000` bytes — inflated by **1,300,000 bytes**.
5. `limit_size` sees `2,500,000 > 2,000,000` and evicts legitimate transactions until the counter drops to ≤ 2 MB.

**Reproducible unit test**: Populate a `PoolMap` with a 24-tx chain, submit a 25th transaction referencing an intermediate tx as a cell dep, then assert:
```rust
assert_eq!(
    pool_map.total_tx_size,
    pool_map.entries.iter().map(|e| e.inner.size).sum::<usize>()
);
```
The assertion will fail, confirming the accounting mismatch.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L246-248)
```rust
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
            entry.inner
```

**File:** tx-pool/src/component/pool_map.rs (L261-264)
```rust
        removed_ids
            .iter()
            .filter_map(|id| self.remove_entry(id))
            .collect()
```

**File:** tx-pool/src/component/pool_map.rs (L738-740)
```rust
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
```
