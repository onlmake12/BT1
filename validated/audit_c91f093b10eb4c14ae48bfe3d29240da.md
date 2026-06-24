Audit Report

## Title
`total_tx_size` / `total_tx_cycles` Inflated by Stale Snapshot Overwrite When Evictions Occur in `add_entry` — (`tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, `updated_stat_for_add_tx` captures a pre-eviction snapshot of `self.total_tx_size + entry.size` into a local variable before `check_and_record_ancestors` runs. When that function evicts transactions via `remove_entry_and_descendants`, `update_stat_for_remove_tx` correctly decrements `self.total_tx_size` in-place. However, lines 218–219 unconditionally overwrite those decremented values with the stale pre-eviction snapshot, permanently inflating the pool's accounting. The inflation is permanent, compounds with each triggering submission, and enables an unprivileged attacker to force unnecessary eviction of legitimate transactions and premature rejection of valid submissions.

## Finding Description

In `add_entry` (L200–221):

- **L210–211**: `updated_stat_for_add_tx(entry.size, entry.cycles)` computes `self.total_tx_size + entry.size` and stores the result in the local variable `total_tx_size`. [1](#0-0) 

- **L213**: `check_and_record_ancestors` is called. When `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (L603), it enters the eviction branch and calls `remove_entry_and_descendants` in a loop (L618). [2](#0-1) 

- `remove_entry_and_descendants` internally calls `update_stat_for_remove_tx`, which decrements `self.total_tx_size` and `self.total_tx_cycles` in-place (L738–740). [3](#0-2) 

- **L218–219**: The stale local variables (computed before any evictions) unconditionally overwrite the correctly-decremented `self.total_tx_size` and `self.total_tx_cycles`, discarding all in-place decrements from evictions. [4](#0-3) 

The net result per triggering submission:
```
final total_tx_size = original_total + entry.size          (actual, wrong)
correct value       = original_total - evicted_sizes + entry.size
```

`updated_stat_for_add_tx` only guards against integer overflow; it does not account for post-eviction state. [5](#0-4)  The in-place decrements from `update_stat_for_remove_tx` are simply discarded by the overwrite.

## Impact Explanation

`total_tx_size` drives two critical behaviors:

1. **`limit_size` (L298)**: evicts transactions while `total_tx_size > max_tx_pool_size`. An inflated counter causes legitimate, already-accepted transactions to be unnecessarily evicted. [6](#0-5) 

2. **`updated_stat_for_add_tx`**: rejects new submissions with `Reject::Full` when `total_tx_size` would overflow. An inflated counter causes premature rejection of valid transactions even when the pool has real capacity. [5](#0-4) 

The inflation is permanent until node restart and compounds with each triggering submission. This maps to **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs** — an attacker can repeatedly inflate the counter to force eviction of honest transactions and block new valid submissions, degrading mempool utility across the network.

## Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged user via the `send_transaction` RPC. Required conditions: the new transaction's ancestor count exceeds `max_ancestors_count` (default 25), and enough ancestors are "cell-ref parents" (share a cell dep with the new transaction's ancestors) such that `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. [7](#0-6)  An attacker can deliberately craft this by submitting a chain of 26+ transactions all referencing a shared cell dep, then submitting a spending transaction also referencing that dep. This is repeatable with only transaction fees, requires no privileged access, key material, or majority hashpower, and each iteration inflates the counter by the evicted transactions' sizes.

## Recommendation

Move `updated_stat_for_add_tx` to execute **after** `check_and_record_ancestors` completes, so it reads the already-updated (post-eviction) `self.total_tx_size` and `self.total_tx_cycles`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Recompute AFTER evictions have already updated self.total_tx_size / total_tx_cycles
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, separate the overflow pre-flight check from the final assignment, and always derive final totals from the post-eviction state.

## Proof of Concept

1. Start node with empty pool (`total_tx_size = 0`, `max_ancestors_count = 25`).
2. Submit tx₁ → tx₂ → … → tx₂₆, each referencing shared cell dep `D`, each 200 bytes. Pool holds 26 entries; `total_tx_size = 5200`.
3. Submit tx₂₇ spending tx₂₆'s output and referencing `D`. `ancestors_count = 27 > 25`; `cell_ref_parents = {tx₁…tx₂₆}`; `27 − 26 = 1 ≤ 25` → eviction branch triggers. [8](#0-7) 
4. `updated_stat_for_add_tx` captures `total_tx_size_local = 5200 + 200 = 5400`.
5. `check_and_record_ancestors` evicts tx₁ (200 bytes): `self.total_tx_size` decremented to `5000`.
6. tx₂₇ inserted. Lines 218–219 assign `self.total_tx_size = 5400` (overwrites `5000`). [4](#0-3) 
7. Pool holds 26 entries (tx₂…tx₂₇) = 5200 actual bytes, but `total_tx_size = 5400` — inflated by 200 bytes.
8. Repeating this pattern accumulates inflation. Once `total_tx_size` exceeds `max_tx_pool_size`, `limit_size` begins evicting honest transactions; `updated_stat_for_add_tx` begins rejecting valid submissions with `Reject::Full`.

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

**File:** tx-pool/src/pool.rs (L297-299)
```rust
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
```
