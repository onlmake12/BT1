The code confirms the claim exactly. Here is the validated audit report:

Audit Report

## Title
Global `total_tx_size`/`total_tx_cycles` Overwritten After Eviction in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, the updated counter values are snapshotted into local variables before `check_and_record_ancestors` runs. If that function evicts transactions (via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`), the correct in-place decrements to `self.total_tx_size`/`self.total_tx_cycles` are immediately overwritten by the stale pre-eviction locals at lines 218–219. The evicted transactions' sizes and cycles are never reflected in the final counters, leaving them permanently inflated.

## Finding Description
In `add_entry` (lines 200–221):

1. **Lines 210–211**: `updated_stat_for_add_tx` computes `total_tx_size = self.total_tx_size + entry.size` and `total_tx_cycles = self.total_tx_cycles + entry.cycles` into **local variables**, snapshotting the pre-eviction state.
2. **Line 213**: `check_and_record_ancestors` is called. When `ancestors_count > max_ancestors_count` and `ancestors_count.saturating_sub(cell_ref_parents.len()) <= max_ancestors_count` (lines 603–625), it calls `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in-place (lines 738–740).
3. **Lines 218–219**: The stale local variables (computed before eviction) are unconditionally written back to `self`, silently discarding the decrements applied in step 2.

The `recompute_total_stat` fallback (lines 743–749) only triggers on underflow, not on this inflation path, so no existing guard catches the overwrite.

## Impact Explanation
The inflated counters cause: (1) `tx_pool_info` RPC to return incorrect `total_tx_size`/`total_tx_cycles`, misleading node operators; (2) `limit_size` to prematurely evict valid fee-paying transactions from the mempool; (3) `updated_stat_for_add_tx` to prematurely reject new transactions with `Reject::Full`. These impacts are local to a single node's mempool and match **Low (501–2000 points): Any other important performance improvements for CKB**.

## Likelihood Explanation
An unprivileged user can trigger this via the `send_transaction` RPC or P2P relay by submitting a chain of transactions that all reference the same cell dep, exceeding `max_ancestors_count`. Each submission that triggers the eviction branch at lines 603–625 inflates the counters by the evicted transaction's size and cycles. Repeated submissions compound the inflation monotonically. No special privileges are required.

## Recommendation
Move the counter update to **after** `check_and_record_ancestors` completes, so evictions are already reflected in `self.total_tx_size`/`self.total_tx_cycles` before the new entry's contribution is added:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

The overflow pre-check (`updated_stat_for_add_tx`) should be moved to after eviction or adjusted to subtract evicted sizes/cycles before writing back.

## Proof of Concept
1. Configure a node with `max_ancestors_count = N`.
2. Submit a chain of `N` transactions `T_1 → T_2 → … → T_N`, all referencing the same cell dep `D`. Record `total_tx_size` via `tx_pool_info` RPC.
3. Submit `T_new` spending an output of `T_N` and also referencing `D`. Its ancestor count = N+1 > `max_ancestors_count`, but `cell_ref_parents` contains the chain txs, satisfying the eviction branch condition (line 603).
4. `check_and_record_ancestors` evicts one chain tx (e.g., `T_1`, size=S, cycles=C) via `remove_entry_and_descendants` → `update_stat_for_remove_tx` (lines 738–740).
5. Lines 218–219 overwrite `self.total_tx_size` with the pre-eviction snapshot + `T_new.size`, ignoring the decrement of S.
6. Query `tx_pool_info`: `total_tx_size` is inflated by S compared to the actual sum of all entries in the pool.
7. Repeat with new transactions to compound the inflation and eventually trigger premature eviction of unrelated transactions via `limit_size`.