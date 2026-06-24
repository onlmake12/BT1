Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Stale Snapshot Overwrite After In-Flight Evictions in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, `updated_stat_for_add_tx` computes a pre-eviction snapshot of `total_tx_size`/`total_tx_cycles` before `check_and_record_ancestors` runs. If `check_and_record_ancestors` evicts cell-ref-parent transactions via `remove_entry_and_descendants` → `update_stat_for_remove_tx`, those decrements are applied directly to `self.total_tx_size`/`self.total_tx_cycles`. The stale pre-eviction snapshot is then unconditionally written back at lines 218–219, permanently overwriting the decrements. Each eviction event inflates `total_tx_size` by the aggregate size of all evicted transactions, causing `limit_size` to over-evict legitimate pool entries on every subsequent insertion.

## Finding Description

The exact sequence in `add_entry` (lines 200–221):

```
L210-211: (total_tx_size, total_tx_cycles) = updated_stat_for_add_tx(entry.size, entry.cycles)
          // Pure read: returns self.total_tx_size + new_size (no mutation)

L213:     evicts = check_and_record_ancestors(&mut entry)
          // May call remove_entry_and_descendants → update_stat_for_remove_tx
          // update_stat_for_remove_tx DIRECTLY writes: self.total_tx_size -= evicted_size

L218-219: self.total_tx_size  = total_tx_size   // ← stale pre-eviction snapshot
          self.total_tx_cycles = total_tx_cycles // ← stale pre-eviction snapshot
```

`updated_stat_for_add_tx` (lines 711–729) is a pure read returning `self.total_tx_size + tx_size` without mutating state. [1](#0-0) 

`update_stat_for_remove_tx` (lines 733–758) directly mutates `self.total_tx_size` and `self.total_tx_cycles`. [2](#0-1) 

`check_and_record_ancestors` (lines 588–640) calls `remove_entry_and_descendants` in a loop for each evicted cell-ref parent (line 618), each of which triggers `update_stat_for_remove_tx`. [3](#0-2) 

After evictions, the correct value should be:
```
self.total_tx_size = old_total − Σ(evicted_sizes) + new_entry_size
```
But lines 218–219 write back:
```
self.total_tx_size = old_total + new_entry_size   // pre-eviction snapshot
```
The delta `Σ(evicted_sizes)` is permanently added to `total_tx_size` for every insertion that triggers the eviction path. [4](#0-3) 

`limit_size` enforces the pool cap by looping while `self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting lowest-fee-rate entries until the condition is false. [5](#0-4) 

Because `total_tx_size` is inflated, `limit_size` evicts additional legitimate transactions on every subsequent call, compounding the error over time.

## Impact Explanation

An unprivileged attacker can permanently inflate `total_tx_size` by a controlled amount per trigger. Once `total_tx_size` is sufficiently inflated above `max_tx_pool_size`, `limit_size` continuously evicts the lowest-fee-rate transactions from the pool even when real capacity is available, effectively denying pool admission to legitimate transactions. This constitutes a low-cost, repeatable denial-of-service against the mempool, matching the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs (10001–15000 points)**.

## Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged RPC caller or P2P relay peer. No special privilege, key material, or majority hash power is required. The attacker submits a batch of transactions that all reference the same cell as a `cell_dep`, then submits a transaction that consumes that cell as an input. This is a documented, tested code path (`TxPoolLimitAncestorCount` integration test). The attack is repeatable: each round inflates `total_tx_size` by `Σ size(evicted_k)`, and rounds can be chained until the counter exceeds `max_tx_pool_size`.

## Recommendation

Move the write-back of `total_tx_size`/`total_tx_cycles` to after all evictions complete, using the **current** (post-eviction) value of `self.total_tx_size` rather than the pre-eviction snapshot:

```rust
// Remove the pre-eviction snapshot entirely:
// let (total_tx_size, total_tx_cycles) = self.updated_stat_for_add_tx(...)?;

// Validate overflow before evictions (keep the check, discard the snapshot):
self.updated_stat_for_add_tx(entry.size, entry.cycles)?;  // error check only

evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Apply addition to the current (post-eviction) value:
self.total_tx_size = self.total_tx_size.checked_add(entry.size)
    .ok_or_else(|| Reject::Full(...))?;
self.total_tx_cycles = self.total_tx_cycles.checked_add(entry.cycles)
    .ok_or_else(|| Reject::Full(...))?;
```

Alternatively, call `recompute_total_stat()` after all mutations to restore consistency, though this is O(n) in pool size.

## Proof of Concept

1. Submit `N` transactions (`tx_ref_1 … tx_ref_N`) that all use `cell_X` as a `cell_dep`. Each is accepted; `total_tx_size` grows normally.
2. Submit `tx_consume` that spends `cell_X` as an **input**. This triggers `check_and_record_ancestors`, which finds `N` cell-ref parents exceeding `max_ancestors_count` and evicts `k = N − max_ancestors_count` of them via `remove_entry_and_descendants` → `update_stat_for_remove_tx`. `self.total_tx_size` is decremented by `Σ size(evicted_k)`.
3. `add_entry` then writes back `total_tx_size = pre_eviction_snapshot + size(tx_consume)`, overwriting the decrements. `total_tx_size` is now inflated by `Σ size(evicted_k)`.
4. Call `tx_pool_info` RPC: `total_tx_size` reports a value larger than the sum of all entries actually in the pool.
5. Repeat steps 1–3 to accumulate inflation until `total_tx_size > max_tx_pool_size` even though the pool is nearly empty. Subsequent `limit_size` calls evict legitimate transactions, denying pool service.

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

**File:** tx-pool/src/component/pool_map.rs (L615-625)
```rust
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

**File:** tx-pool/src/pool.rs (L297-327)
```rust
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
```
