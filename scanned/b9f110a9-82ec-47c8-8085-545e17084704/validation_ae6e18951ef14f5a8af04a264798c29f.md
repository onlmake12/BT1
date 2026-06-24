Audit Report

## Title
Stale Aggregate Counter Overwrite After Ancestor-Eviction in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, the local variables `total_tx_size` and `total_tx_cycles` are computed as a pre-eviction snapshot at lines 210–211, before `check_and_record_ancestors` (line 213) may evict transactions via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`. The stale snapshot is then unconditionally written back to `self.total_tx_size` / `self.total_tx_cycles` at lines 218–219, silently undoing every decrement performed during eviction. This permanently inflates the pool's size and cycle counters until the node restarts, causing spurious downstream evictions and false `Reject::Full` responses.

## Finding Description

`add_entry` in `pool_map.rs` follows this exact sequence: [1](#0-0) 

`updated_stat_for_add_tx` computes `self.total_tx_size + entry.size` and `self.total_tx_cycles + entry.cycles` and stores them in local variables — a snapshot of the pre-eviction state. [2](#0-1) 

`check_and_record_ancestors` may enter the eviction branch when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`: [3](#0-2) 

Each `remove_entry_and_descendants` call chains to `remove_entry`, which calls `update_stat_for_remove_tx` and correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in place: [4](#0-3) [5](#0-4) 

After `check_and_record_ancestors` returns, the stale local snapshot is written back unconditionally: [6](#0-5) 

This overwrites the correctly-decremented `self.total_tx_size` with `old_total + new_entry_size`, effectively re-adding the sizes of all evicted transactions. The inflation is permanent for the lifetime of the pool.

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

1. **Spurious eviction loop**: `limit_size` loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`. [7](#0-6)  An inflated counter causes the loop to evict additional valid, fee-paying transactions that should remain in the pool, degrading pool quality and starving legitimate users.

2. **False `Reject::Full`**: `updated_stat_for_add_tx` rejects new submissions when the inflated `total_tx_size` appears to exceed capacity, even though the real pool is well within limits. [8](#0-7) 

3. **Incorrect RPC reporting**: `total_tx_size` and `total_tx_cycles` are surfaced directly via `tx_pool_info` RPC, misleading operators and downstream tooling. [9](#0-8) 

The attack is repeatable: each triggering submission inflates the counters further, and the attacker can drive the node into a state where it rejects all new transactions with `Reject::Full` despite the pool being nearly empty.

## Likelihood Explanation

Any unprivileged user with access to `send_transaction` RPC or P2P relay can trigger this. The required setup — N > `max_ancestors_count` pooled transactions sharing a common `cell_dep` output, followed by a transaction that consumes that output as an input — is explicitly demonstrated in the integration test `TxPoolLimitAncestorCount` in `test/src/specs/tx_pool/limit.rs` (lines 70–127). No special privilege, key material, or majority hashpower is required. The attack cost is only the transaction fees for the crafted chain.

## Recommendation

Move the snapshot computation to **after** `check_and_record_ancestors` completes, so it reads the already-correct (post-eviction) `self.total_tx_size`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
// self.total_tx_size already reflects evictions; add only the new entry
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

This ensures the final written values are always `post_eviction_total + new_entry_contribution`, with no stale snapshot possible.

## Proof of Concept

1. Configure a node with default `max_ancestors_count` (e.g., 125 or 1000 per the test).
2. Submit N > `max_ancestors_count` transactions, each spending an independent input but all referencing the same `cell_dep` output (cell-ref parents). All are accepted into the pool.
3. Submit a new transaction whose **input** consumes that same cell output. Its ancestor count = N + 1 > `max_ancestors_count`, but `(N+1) - N = 1 <= max_ancestors_count`, so the eviction branch fires.
4. `check_and_record_ancestors` evicts `(N + 1 - max_ancestors_count)` cell-ref transactions, each correctly decrementing `self.total_tx_size`.
5. Lines 218–219 overwrite `self.total_tx_size` with the stale snapshot = `old_total + new_entry_size`, which does not reflect the evictions.
6. Query `tx_pool_info` RPC: `total_tx_size` is inflated by `sum(evicted_tx_sizes)`.
7. Repeat step 3 with additional transactions to drive `total_tx_size` above `max_tx_pool_size`; observe that `limit_size` begins evicting valid transactions and new submissions receive `Reject::Full` despite the pool being underfull.

The integration test `TxPoolLimitAncestorCount` already exercises the eviction path; adding an assertion on `tx_pool_info().total_tx_size == actual_sum_of_pooled_tx_sizes` after the eviction step would reproduce the invariant violation.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L68-71)
```rust
    // sum of all tx_pool tx's virtual sizes.
    pub(crate) total_tx_size: usize,
    // sum of all tx_pool tx's cycles.
    pub(crate) total_tx_cycles: Cycle,
```

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

**File:** tx-pool/src/component/pool_map.rs (L716-721)
```rust
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_size {} overflows by add {}",
                self.total_tx_size, tx_size
            ))
        })?;
```

**File:** tx-pool/src/component/pool_map.rs (L733-741)
```rust
    fn update_stat_for_remove_tx(&mut self, tx_size: usize, cycles: Cycle) {
        match (
            self.total_tx_size.checked_sub(tx_size),
            self.total_tx_cycles.checked_sub(cycles),
        ) {
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
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
