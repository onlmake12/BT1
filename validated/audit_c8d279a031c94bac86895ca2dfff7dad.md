Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Inflated After Ancestor-Eviction in `add_entry` — (File: tx-pool/src/component/pool_map.rs)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` captures a pre-eviction snapshot of `total_tx_size`/`total_tx_cycles` into local variables before `check_and_record_ancestors` runs. When `check_and_record_ancestors` evicts `cell_ref_parents` via `remove_entry_and_descendants`, each removal correctly decrements `self.total_tx_size` in-place through `update_stat_for_remove_tx`. However, the pre-eviction snapshot is then unconditionally written back to `self.total_tx_size`, erasing those decrements. The result is a permanently inflated `total_tx_size` that causes `limit_size` to over-evict valid transactions on every subsequent insertion.

## Finding Description
`PoolMap::add_entry` in `tx-pool/src/component/pool_map.rs` (L200–221):

```rust
// Step 1: snapshot pre-eviction totals (self.total_tx_size + entry.size)
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;  // L210-211

// Step 2: may evict N transactions; each calls update_stat_for_remove_tx
//         which DECREMENTS self.total_tx_size and self.total_tx_cycles in-place
evicts = self.check_and_record_ancestors(&mut entry)?;         // L213

// Step 3: unconditionally overwrite with the pre-eviction snapshot
self.total_tx_size = total_tx_size;    // L218 — evictions' decrements are lost
self.total_tx_cycles = total_tx_cycles; // L219
```

`updated_stat_for_add_tx` (L711–729) is a pure read (`&self`): it returns `self.total_tx_size + tx_size` without modifying any state. [1](#0-0) 

`check_and_record_ancestors` (L588–640) enters the eviction branch when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. It calls `remove_entry_and_descendants` (L618), which calls `remove_entry` (L235–250), which calls `update_stat_for_remove_tx` (L733–758), directly mutating `self.total_tx_size`. [2](#0-1) 

After Step 3, `self.total_tx_size` equals `(pre-eviction total) + entry.size`, ignoring all `−size(evicted_tx_i)` terms. The invariant `total_tx_size == Σ entry.size for all entries` is broken. [3](#0-2) 

The exploit path is reachable via the public `send_transaction` RPC → `submit_entry` (process.rs L96) → `_submit_entry` → `add_pending`/`add_gap`/`add_proposed` → `pool_map.add_entry`. After insertion, `limit_size` (pool.rs L292–329) is called unconditionally (process.rs L151) and loops while `self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting valid transactions due to the inflated counter. [4](#0-3) [5](#0-4) 

## Impact Explanation
This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. An attacker broadcasting a single crafted transaction to all reachable nodes simultaneously inflates each node's `total_tx_size`, triggering `limit_size` to evict legitimate pending transactions. Evicted transactions must be resubmitted by their originators, generating rebroadcast traffic. Repeated triggering (each new crafted transaction costs only standard fees) compounds the inflation across the network, degrading mempool utility and increasing resubmission load network-wide. The attacker can selectively target high-value pending transactions for eviction by timing submissions to ensure those transactions are the lowest-fee entries when `limit_size` runs.

## Likelihood Explanation
The trigger requires: (1) existing pool transactions that share a common cell dep output (e.g., a widely-used lock script cell dep, which is routine on mainnet); (2) a new transaction that spends that cell dep as an input, making those transactions `cell_ref_parents`; (3) the resulting ancestor count exceeds `max_ancestors_count` (default 25) but is reducible by evicting some `cell_ref_parents`. Condition (1) is satisfied naturally by any popular lock script. Conditions (2) and (3) are attacker-controlled. No special privileges, keys, or majority hashpower are required — only the ability to submit a valid transaction via `send_transaction`. The attack is repeatable at low cost.

## Recommendation
Move the stat update to after `check_and_record_ancestors` completes, adding only the new entry's contribution on top of the already-correct post-eviction totals:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Apply only the new entry's delta to the post-eviction self.total_tx_*
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, remove the local bindings entirely and call `updated_stat_for_add_tx` after `check_and_record_ancestors`, or add an overflow guard that re-checks against `self.total_tx_size` post-eviction.

## Proof of Concept
**Setup:** `max_ancestors_count = 3`, `max_tx_pool_size = 300`. Insert transactions A (size=100), B (size=100), C (size=100) all referencing cell dep output `X`. `total_tx_size = 300`.

**Attack:** Submit transaction D (size=50) that spends cell dep `X` as an input. D has 3 `cell_ref_parents` (A, B, C) → `ancestors_count = 4 > 3`. `check_and_record_ancestors` evicts A: `update_stat_for_remove_tx(100)` → `self.total_tx_size = 200`. Step 3 then writes `self.total_tx_size = 300 + 50 = 350`.

**Result:** Pool contains {B, C, D}, actual total size = 250, but `total_tx_size = 350`. `limit_size` sees `350 > 300` and evicts B or C unnecessarily. Actual pool size after `limit_size` = 150 (only C and D remain), even though the pool was within limits after the correct accounting would give 250.

**Reproducible test:** Write a unit test in `tx-pool/src/component/tests/` using `PoolMap::new(3)`, insert three transactions sharing a cell dep, insert a fourth that spends that cell dep as an input, assert `pool_map.total_tx_size == actual_sum_of_entry_sizes` after `add_entry` returns.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
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

**File:** tx-pool/src/process.rs (L150-152)
```rust
                tx_pool
                    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
                    .map_or(Ok(()), Err)?;
```

**File:** tx-pool/src/pool.rs (L292-299)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
```
