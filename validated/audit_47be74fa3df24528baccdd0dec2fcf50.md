The code confirms all claims. The bug is real and the full call chain is verified:

- [1](#0-0)  `updated_stat_for_add_tx` takes `&self` (read-only), capturing pre-eviction snapshots into locals.
- [2](#0-1)  `check_and_record_ancestors` is called next and can evict via `remove_entry_and_descendants`.
- [3](#0-2)  `remove_entry` calls `update_stat_for_remove_tx`, correctly decrementing `self.total_tx_size` in place.
- [4](#0-3)  The stale locals are then unconditionally written back, overwriting the correctly-decremented values.
- [5](#0-4)  The eviction path is reachable by any unprivileged submitter.
- [6](#0-5)  `limit_size()` uses the inflated `total_tx_size` as its sole guard.

---

Audit Report

## Title
`total_tx_size` / `total_tx_cycles` Inflated When Evictions Occur During `add_entry` - (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, pre-eviction snapshots of `total_tx_size` and `total_tx_cycles` are captured into local variables before `check_and_record_ancestors` runs. When that function evicts transactions, `update_stat_for_remove_tx` correctly decrements the fields in place. However, lines 218–219 then unconditionally overwrite those fields with the stale pre-eviction snapshots, permanently inflating the pool's accounting by the sum of all evicted transactions' sizes and cycles. The inflation accumulates with each triggering insertion and persists until node restart.

## Finding Description
The exact sequence in `add_entry` (lines 200–221 of `tx-pool/src/component/pool_map.rs`):

1. **Lines 210–211**: `updated_stat_for_add_tx` is a `&self` (read-only) method. It returns `(self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles)` into local variables without modifying any state.

2. **Line 213**: `check_and_record_ancestors` is called. When `ancestors_count > max_ancestors_count` but reducible via `cell_ref_parents`, it calls `remove_entry_and_descendants` (lines 618–621), which calls `remove_entry` for each evicted tx. `remove_entry` calls `update_stat_for_remove_tx` at line 247, which **correctly decrements** `self.total_tx_size` and `self.total_tx_cycles` in place.

3. **Lines 218–219**: The stale local variables — computed before any evictions — are written back unconditionally, overwriting the correctly-decremented `self.total_tx_size` and `self.total_tx_cycles`.

After this sequence, `self.total_tx_size = old_total + new_entry_size` instead of the correct `old_total - sum(evicted_sizes) + new_entry_size`. The eviction path is confirmed reachable at lines 603–625.

## Impact Explanation
`total_tx_size` is the sole guard in `limit_size()` (`pool.rs` line 298) that triggers eviction of legitimate pool transactions. An inflated `total_tx_size` causes `limit_size()` to evict legitimate transactions even when the pool has real capacity. Additionally, `updated_stat_for_add_tx` uses `total_tx_size` to reject new submissions with `Reject::Full` via overflow check. Because the inflation accumulates with each triggering insertion, an attacker broadcasting crafted transactions to all reachable nodes can progressively shrink the effective mempool capacity across the network, causing widespread rejection and eviction of legitimate transactions. This matches the **High** impact: **Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
The eviction path in `check_and_record_ancestors` is triggered when a submitted transaction references a cell dep whose output is already consumed by a pool transaction (`cell_ref_parents`), and the ancestor count exceeds `max_ancestors_count`. This is reachable by any unprivileged submitter via RPC `send_transaction` or P2P relay. No special privilege is required. The attacker can craft a sequence of transactions that repeatedly trigger this path, accumulating inflation with each submission. The attack is repeatable and low-cost.

## Recommendation
Remove the pre-eviction snapshot approach. After `check_and_record_ancestors` returns, `self.total_tx_size` is already correctly decremented for evictions — simply add the new entry's contribution:

```rust
// Replace lines 210-211 and 218-219 with:
// (remove the pre-eviction call to updated_stat_for_add_tx)
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Now apply the additive update on the already-correct self.total_tx_size:
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

The overflow/full check from `updated_stat_for_add_tx` should be preserved but applied after evictions complete, using the post-eviction `self.total_tx_size`.

## Proof of Concept
1. Fill the pool with a chain of transactions up to `max_ancestors_count - 1` ancestors, where the last transaction in the chain uses a cell dep output (`cell_ref_parent`). Record `total_tx_size` via `tx_pool_info` RPC.
2. Submit a new transaction that also references that same cell dep output as an ancestor, pushing `ancestors_count` above `max_ancestors_count`.
3. `check_and_record_ancestors` evicts the `cell_ref_parent` transaction (and its descendants) via `remove_entry_and_descendants`, correctly decrementing `self.total_tx_size`.
4. `add_entry` then overwrites `self.total_tx_size` with the stale pre-eviction value.
5. Query `tx_pool_info` via RPC: `total_tx_size` is now larger than the sum of actual pool entries' sizes by `sum(evicted_tx.size)`.
6. Repeat steps 1–5: each triggering insertion inflates `total_tx_size` further. Eventually `limit_size()` evicts legitimate transactions and `updated_stat_for_add_tx` falsely rejects new submissions as `Reject::Full`, even when the pool has real capacity.

A unit test can assert the invariant: after `add_entry` returns, `pool_map.total_tx_size == pool_map.entries.iter().map(|e| e.size).sum()`. This invariant is violated whenever the eviction path in `check_and_record_ancestors` is taken.

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
