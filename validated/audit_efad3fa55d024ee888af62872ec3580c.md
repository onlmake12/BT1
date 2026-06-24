The code confirms the claim exactly. Let me verify the `limit_size` function and the `remove_entry_and_descendants` call chain to complete the analysis.

Audit Report

## Title
Stale Pre-Computed `total_tx_size`/`total_tx_cycles` Overwrites In-Place Eviction Updates — (`tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` captures a snapshot of the future totals before ancestor eviction occurs. `check_and_record_ancestors` may then call `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which directly mutates `self.total_tx_size` and `self.total_tx_cycles` in-place. The final unconditional assignment at lines 218–219 overwrites those in-place mutations with the stale pre-eviction snapshot, permanently inflating the pool's size accounting. This causes `limit_size` to fire spuriously, evicting valid transactions from the mempool.

## Finding Description
The root cause is in `add_entry` (`pool_map.rs` lines 200–221):

**Step 1 (line 210–211):** `updated_stat_for_add_tx` takes `&self` (immutable borrow) and returns `(self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles)` as a snapshot. No mutation occurs yet. [1](#0-0) 

**Step 2 (line 213):** `check_and_record_ancestors` takes `&mut self` and, when `ancestors_count > max_ancestors_count` and the eviction branch at line 603 is entered, calls `remove_entry_and_descendants` (line 618) → `remove_entry` (line 263) → `update_stat_for_remove_tx` (line 247), which **directly subtracts** evicted sizes/cycles from `self.total_tx_size` and `self.total_tx_cycles`. [2](#0-1) [3](#0-2) 

**Step 3 (lines 218–219):** The stale snapshot from Step 1 is unconditionally written back, erasing all subtractions performed in Step 2. [4](#0-3) 

Concrete trace (tx A: size=100 in pool; tx B: size=50 being added, triggers eviction of A):

| Step | `self.total_tx_size` |
|---|---|
| Before add_entry | 100 |
| After Step 1 (snapshot) | snapshot=150 |
| After Step 2 (A evicted via `update_stat_for_remove_tx`) | 0 |
| After Step 3 (overwrite) | **150** (wrong; correct is 50) |

No existing guard prevents this: `updated_stat_for_add_tx` only checks for integer overflow, not for the ordering problem. [5](#0-4) 

## Impact Explanation
The inflated `total_tx_size` causes `limit_size` (pool.rs line 298) to loop `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting valid, fee-paying transactions even when the pool is actually under its configured limit. [6](#0-5) 

The inflation is **permanent** per node restart and **compounds** with each eviction-triggering submission. A sustained low-cost attack keeps the mempool in a state of continuous spurious eviction, preventing honest users' transactions from being accepted or retained. This matches the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* The attacker spends only the cost of crafting valid transactions with the right cell-dep structure; no privileged access, leaked keys, or hashpower is required.

## Likelihood Explanation
The eviction branch in `check_and_record_ancestors` (line 603) is reachable by any unprivileged user who submits a transaction via `send_transaction` RPC or P2P relay satisfying:
- `ancestors_count > max_ancestors_count`, AND
- `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`

This is a standard, supported transaction pattern (cell deps referencing in-pool outputs). The attacker needs only to pre-place a transaction in the pool and then submit a second transaction referencing it as a cell dep with the right ancestor count. The attack is repeatable and compounds monotonically. [7](#0-6) 

## Recommendation
Move the size/cycles accounting update to **after** `check_and_record_ancestors` returns, so evictions are already reflected before the new entry's contribution is added:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Add only the new entry's contribution after evictions are settled
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, replace `updated_stat_for_add_tx` with a mutable in-place `update_stat_for_add_tx` called after `check_and_record_ancestors`, mirroring the existing `update_stat_for_remove_tx` pattern. [8](#0-7) 

## Proof of Concept
Deterministic reasoning (no external tooling required):

1. Pre-populate pool with tx A (size=100, cycles=1000). `total_tx_size = 100`.
2. Submit tx B (size=50, cycles=500) with a cell dep on A's output, with `ancestors_count = max_ancestors_count + 1` and `cell_ref_parents = {A}`, satisfying the eviction branch condition at line 603.
3. `updated_stat_for_add_tx` snapshots `total_tx_size = 150` (line 210–211).
4. `check_and_record_ancestors` evicts A: `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx(100, 1000)` → `self.total_tx_size = 0` (line 247).
5. Line 218 sets `self.total_tx_size = 150` (stale snapshot).
6. Pool now contains only tx B (size=50) but reports `total_tx_size = 150`.
7. `limit_size` loop fires if `max_tx_pool_size < 150`, evicting tx B even though the pool is actually under the limit.
8. Repeat submissions compound the inflation monotonically. [9](#0-8) [10](#0-9)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L200-221)
```rust
    pub(crate) fn add_entry(
        &mut self,
        mut entry: TxEntry,
        status: Status,
    ) -> Result<(bool, HashSet<TxEntry>), Reject> {
        let tx_short_id = entry.proposal_short_id();
        let mut evicts = Default::default();
        if self.entries.get_by_id(&tx_short_id).is_some() {
            return Ok((false, evicts));
        }
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
        Ok((true, evicts))
    }
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

**File:** tx-pool/src/component/pool_map.rs (L733-758)
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
            _ => {
                if let Some((total_tx_size, total_tx_cycles)) = self.recompute_total_stat() {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, recomputed size {} cycles {}",
                        tx_size, cycles, total_tx_size, total_tx_cycles
                    );
                    self.total_tx_size = total_tx_size;
                    self.total_tx_cycles = total_tx_cycles;
                } else {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, and recomputing overflowed",
                        tx_size, cycles
                    );
                }
            }
        }
    }
```

**File:** tx-pool/src/pool.rs (L298-328)
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
        ret
```
