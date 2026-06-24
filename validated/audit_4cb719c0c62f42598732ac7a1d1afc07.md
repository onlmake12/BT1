Audit Report

## Title
`add_entry()` Overwrites Post-Eviction Pool Size/Cycle Totals with Stale Pre-Eviction Snapshot — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry()`, `total_tx_size` and `total_tx_cycles` are snapshotted via `updated_stat_for_add_tx` before ancestor-eviction occurs. When `check_and_record_ancestors` triggers eviction via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, the in-place subtractions to `self.total_tx_size` and `self.total_tx_cycles` are immediately overwritten by the stale pre-eviction snapshot on lines 218–219. Each eviction event permanently inflates the pool's accounting totals by the sizes and cycles of every evicted transaction, causing premature pool-full rejections of legitimate transactions.

## Finding Description

The exact code sequence in `add_entry()` is: [1](#0-0) 

1. **Lines 210–211**: `updated_stat_for_add_tx` computes `total_tx_size = self.total_tx_size + entry.size` and `total_tx_cycles = self.total_tx_cycles + entry.cycles` as a local snapshot. At this point no eviction has occurred.

2. **Line 213**: `check_and_record_ancestors` may enter the eviction branch when `ancestors_count > max_ancestors_count` but `cell_ref_parents` can bring it within limits: [2](#0-1) 
   This calls `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which correctly modifies `self.total_tx_size` and `self.total_tx_cycles` in place: [3](#0-2) [4](#0-3) 

3. **Lines 218–219**: The stale pre-eviction snapshot is unconditionally written back, overwriting the correct post-eviction values. The net effect: `self.total_tx_size` is inflated by the sum of all evicted transactions' sizes, and similarly for cycles.

`updated_stat_for_add_tx` only checks for integer overflow (`checked_add`), not against `max_tx_pool_size`: [5](#0-4) 

The pool-full enforcement and RPC reporting both consume `total_tx_size` and `total_tx_cycles` directly: [6](#0-5) 

Each eviction-triggering `add_entry` call leaves a permanent ghost inflation equal to the total size/cycles of evicted entries. The inflation is cumulative across repeated eviction events.

## Impact Explanation

**High** — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.

After each eviction-triggering `add_entry`, the node's `total_tx_size` is permanently inflated. Over repeated eviction events the reported pool size diverges arbitrarily far above the true occupancy. Once the inflated counter exceeds `max_tx_pool_size`, every subsequent `send_transaction` RPC call returns `Reject::Full` regardless of actual pool occupancy. An attacker can repeatably trigger this with crafted transactions (no special privileges required), effectively disabling the mempool's admission path on targeted nodes. Nodes that cannot accept new transactions stop propagating them, contributing to network-level congestion.

## Likelihood Explanation

**Medium** — The eviction branch in `check_and_record_ancestors` requires: (1) a new transaction whose ancestor count exceeds `max_ancestors_count`, and (2) some ancestors are `cell_ref_parents` (transactions using a cell as `cell_dep` that the new transaction spends as input). An unprivileged caller can deliberately construct this scenario via `send_transaction`: first submit transactions referencing a specific live cell as `cell_dep`, then submit a long-chain transaction spending that cell as input. This is repeatable and requires no special privileges or leaked keys.

## Recommendation

Remove the pre-eviction snapshot pattern. Instead, apply the new entry's contribution to `self.total_tx_size` and `self.total_tx_cycles` directly after all evictions have completed:

```rust
// After check_and_record_ancestors, record_entry_edges, insert_entry, etc.
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

The pre-eviction call to `updated_stat_for_add_tx` should be retained only for its overflow/limit validation, but its returned snapshot values must not be written back to `self`.

## Proof of Concept

**Setup:** `max_ancestors_count = 25`, pool holds 20 transactions (total size = 3,000 bytes), some referencing `cell_X` as `cell_dep`.

1. Submit `tx_A` (size = 500 bytes) spending `cell_X` as input with 24 in-pool ancestors. `ancestors_count = 25 > max_ancestors_count`, but `cell_ref_parents` can be evicted.
2. Three `cell_ref_parent` transactions (total size = 1,500 bytes) are evicted. `update_stat_for_remove_tx` correctly sets `self.total_tx_size = 3,000 − 1,500 = 1,500`.
3. Lines 218–219 overwrite: `self.total_tx_size = 3,000 + 500 = 3,500` (stale snapshot). Correct value: `1,500 + 500 = 2,000`.
4. Ghost inflation of 1,500 bytes persists permanently.
5. Repeat ~5 times. `total_tx_size` exceeds `max_tx_pool_size` while actual pool occupancy remains well below the limit. All subsequent `send_transaction` calls return `Reject::Full`.

A unit test can verify this by calling `add_entry` with a crafted entry that triggers the `cell_ref_parents` eviction branch, then asserting `pool_map.total_tx_size == actual_sum_of_entry_sizes`.

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

**File:** tx-pool/src/component/pool_map.rs (L738-740)
```rust
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/service.rs (L1086-1096)
```rust
            pending_size: tx_pool.pool_map.pending_size(),
            proposed_size: tx_pool.pool_map.proposed_size(),
            orphan_size: orphan.len(),
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
            min_fee_rate: self.tx_pool_config.min_fee_rate,
            min_rbf_rate: self.tx_pool_config.min_rbf_rate,
            last_txs_updated_at: tx_pool.pool_map.get_max_update_time(),
            tx_size_limit: TRANSACTION_SIZE_LIMIT,
            max_tx_pool_size: self.tx_pool_config.max_tx_pool_size as u64,
            verify_queue_size: verify_queue.len(),
```
