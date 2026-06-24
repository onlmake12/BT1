The code confirms the claim exactly. All cited lines match the actual source.

- `add_entry` lines 210–211: pre-computes `total_tx_size`/`total_tx_cycles` as local snapshots. [1](#0-0) 
- `check_and_record_ancestors` line 618: calls `remove_entry_and_descendants`, which calls `remove_entry` → `update_stat_for_remove_tx`, directly mutating `self.total_tx_size`/`self.total_tx_cycles`. [2](#0-1) 
- Lines 218–219: the stale pre-computed values are written back, overwriting the live decrements. [3](#0-2) 
- `limit_size` uses `total_tx_size` as the sole eviction guard. [4](#0-3) 

---

Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Overwritten After Ancestor-Eviction in `add_entry` — (`tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::add_entry` snapshots `total_tx_size`/`total_tx_cycles` before calling `check_and_record_ancestors`, which can evict pool entries and correctly decrement those fields via `update_stat_for_remove_tx`. The stale snapshot is then unconditionally written back at lines 218–219, silently undoing every decrement. The result is a permanent, cumulative inflation of both counters that causes the pool to believe it is larger than it actually is, leading to spurious eviction of valid transactions and rejection of new submissions with `Reject::Full`.

## Finding Description
In `add_entry` (lines 200–221 of `tx-pool/src/component/pool_map.rs`):

```
L210-211: (total_tx_size, total_tx_cycles) = self.updated_stat_for_add_tx(...)
           // snapshot: self.total_tx_size + entry.size (before any evictions)

L213:      evicts = self.check_and_record_ancestors(&mut entry)?
           // may call remove_entry_and_descendants(next_id)
           //   → remove_entry(id)
           //     → update_stat_for_remove_tx(size, cycles)
           //       → self.total_tx_size  -= evicted.size   // LIVE write
           //       → self.total_tx_cycles -= evicted.cycles // LIVE write

L218-219:  self.total_tx_size  = total_tx_size;   // OVERWRITES live decrements
           self.total_tx_cycles = total_tx_cycles; // OVERWRITES live decrements
```

The eviction path inside `check_and_record_ancestors` (lines 603–625) is taken when `ancestors_count > max_ancestors_count` but `cell_ref_parents` exist that can be evicted to bring the count within limit. Each call to `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx` correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in-place. However, those decrements are immediately overwritten at lines 218–219 by the stale pre-eviction snapshot. No existing guard prevents this: `updated_stat_for_add_tx` is a pure read-only computation that returns local values, and `update_stat_for_remove_tx` has no mechanism to prevent a subsequent overwrite.

## Impact Explanation
**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

`limit_size` (pool.rs lines 298–326) uses `self.pool_map.total_tx_size > self.config.max_tx_pool_size` as its sole eviction guard. An inflated counter causes it to evict valid, fee-paying transactions even when the pool is actually below capacity. `updated_stat_for_add_tx` uses the same field to gate admission; with an inflated counter, subsequent legitimate transactions are rejected with `Reject::Full`. The inflation is **permanent** (survives until node restart) and **cumulative** (each triggered eviction adds more phantom bytes). An attacker can repeat the trigger cheaply to progressively inflate the counter until the node's mempool is effectively non-functional, rejecting all incoming transactions. This constitutes a low-cost denial-of-service against transaction propagation on targeted nodes.

## Likelihood Explanation
Triggerable by any unprivileged user via the standard `send_transaction` RPC path: `send_transaction` → `submit_entry` → `_submit_entry` → `add_pending` → `pool_map.add_entry`. The required precondition — a transaction whose ancestor count exceeds `max_ancestors_count` (default 25) with at least one cell-ref parent — is deliberately constructible: submit a chain of ≥25 transactions where an intermediate transaction holds a cell-dep pointing to an output, then submit a new transaction spending that output as an input. The attacker pays only normal transaction fees for the setup chain. The trigger is repeatable: after each successful trigger, the phantom inflation grows, and the attacker can re-trigger with a new chain.

## Recommendation
Move the stat increment to **after** all mutations complete, operating on the already-updated `self.total_tx_size` (post-eviction):

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Apply new-entry increment only after all evictions have updated the counters:
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, call `recompute_total_stat()` at the end of `add_entry` to reconcile any drift, consistent with the fallback already used in `update_stat_for_remove_tx` (lines 743–749).

## Proof of Concept
1. Configure a node with `max_ancestors_count = 25`.
2. Submit a chain: `tx0 → tx1 → … → tx24`, where `tx5` holds a cell-dep pointing to `output_A`. Record `S = sum of sizes of all 25 txs`.
3. Submit `tx_new` spending `output_A` as an input. Its ancestor count = 26 > 25.
4. `check_and_record_ancestors` detects `cell_ref_parents = {tx5}`, evicts `tx5` and descendants `tx6…tx24` (20 entries), calling `update_stat_for_remove_tx` 20 times → `self.total_tx_size` is correctly reduced to `S - size(tx5…tx24)`.
5. Lines 218–219 write back `total_tx_size = S + size(tx_new)`, ignoring the 20 decrements.
6. **Observed:** `total_tx_size` reports `S + size(tx_new)` (26 txs worth of bytes) while the pool actually contains only 6 transactions (`tx0…tx4` + `tx_new`).
7. `limit_size` immediately evicts more valid transactions to bring the phantom counter below `max_tx_pool_size`; subsequent `send_transaction` calls return `PoolIsFull` even though the pool is nearly empty.
8. Repeat steps 2–5 to accumulate further phantom inflation.

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

**File:** tx-pool/src/component/pool_map.rs (L616-621)
```rust
            while ancestors_count > self.max_ancestors_count {
                if let Some(next_id) = iter.next() {
                    let removed = self.remove_entry_and_descendants(next_id);
                    ancestors_count = ancestors_count.saturating_sub(1);
                    parents.remove(next_id);
                    evicted.extend(removed);
```

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```
