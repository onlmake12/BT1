Audit Report

## Title
Stale Pre-Eviction Totals Overwrite Eviction-Adjusted `total_tx_size`/`total_tx_cycles` in `PoolMap::add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` computes new totals into local variables before `check_and_record_ancestors` runs. When that function evicts transactions, `update_stat_for_remove_tx` correctly decrements `self.total_tx_size`/`self.total_tx_cycles` in-place. However, lines 218–219 unconditionally overwrite those fields with the stale pre-eviction locals, permanently overstating both counters by the aggregate size and cycles of all evicted transactions. This causes the pool to believe it is fuller than it is, triggering spurious eviction of legitimate transactions and rejection of new submissions.

## Finding Description
The exact sequence in `add_entry` (lines 200–221):

1. **Lines 210–211**: `updated_stat_for_add_tx(entry.size, entry.cycles)` takes `&self` (immutable borrow), reads `self.total_tx_size` and `self.total_tx_cycles`, adds the new entry's contribution, and returns the results as local variables `total_tx_size` / `total_tx_cycles`. `self` is not mutated here. [1](#0-0) 

2. **Line 213**: `check_and_record_ancestors(&mut entry)` is called. When `ancestors_count > max_ancestors_count` but `ancestors_count.saturating_sub(cell_ref_parents.len()) <= max_ancestors_count` (lines 603–625), it calls `remove_entry_and_descendants` → `update_stat_for_remove_tx`, which **directly mutates** `self.total_tx_size` and `self.total_tx_cycles`. [2](#0-1) [3](#0-2) 

3. **Lines 218–219**: `self.total_tx_size = total_tx_size; self.total_tx_cycles = total_tx_cycles;` — the stale pre-eviction locals are written back, silently discarding all eviction-driven decrements. [4](#0-3) 

The `recompute_total_stat` fallback inside `update_stat_for_remove_tx` (lines 742–749) is only triggered on arithmetic underflow (`checked_sub` returning `None`). In this eviction path the subtraction is valid (the evicted entries are present in the pool), so underflow does not occur and the fallback is never reached. No other guard corrects the overstatement. [5](#0-4) 

## Impact Explanation
`total_tx_size` is the sole counter driving two critical admission paths. `limit_size` loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting lowest-fee transactions; an overstated counter causes it to evict legitimate pending/proposed transactions that would otherwise fit. `updated_stat_for_add_tx` rejects incoming transactions with `Reject::Full` on overflow; an overstated counter causes premature rejection of valid submissions. The overstatement accumulates with each crafted submission, producing a sustained, attacker-controlled DoS against the transaction pool that prevents transaction propagation across the network. This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. [6](#0-5) 

## Likelihood Explanation
The eviction path in `check_and_record_ancestors` is reachable by any unprivileged peer via `send_transaction` RPC or P2P relay. The attacker must submit a chain of transactions where several share a cell dep (populating `cell_ref_parents`), then submit a new transaction whose ancestor count exceeds `max_ancestors_count` (default 25) but whose `cell_ref_parents` count is large enough to satisfy `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. This requires no privileged access, no majority hashpower, and no victim mistakes. The overstatement accumulates with each such submission, making the attack repeatable and compounding. [7](#0-6) 

## Recommendation
Remove the pre-computation call to `updated_stat_for_add_tx` before `check_and_record_ancestors`. After `check_and_record_ancestors` returns, apply the new entry's contribution directly on top of the already-eviction-adjusted `self` fields using `checked_add`, returning `Reject::Full` on overflow:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
self.total_tx_size = self.total_tx_size.checked_add(entry.size).ok_or_else(|| {
    Reject::Full(format!("tx-pool total_tx_size overflows by add {}", entry.size))
})?;
self.total_tx_cycles = self.total_tx_cycles.checked_add(entry.cycles).ok_or_else(|| {
    Reject::Full(format!("tx-pool total_tx_cycles overflows by add {}", entry.cycles))
})?;
``` [8](#0-7) 

## Proof of Concept
Concrete accounting trace:

| Step | Event | `self.total_tx_size` |
|---|---|---|
| Initial | Pool has 3 txs, total size 300 | 300 |
| `updated_stat_for_add_tx(50, …)` | Returns local `total_tx_size = 350`; `self` unchanged | 300 |
| `check_and_record_ancestors` evicts 2 txs (80 bytes each) | `update_stat_for_remove_tx` called twice | 300 − 80 − 80 = **140** |
| `self.total_tx_size = total_tx_size` (line 218) | Stale local written back | **350** (correct value: 190) |

Unit test plan: construct a `PoolMap` with `max_ancestors_count = 3`, insert 3 transactions sharing a cell dep (making them `cell_ref_parents`), then insert a 4th transaction that has all 3 as ancestors. After `add_entry` returns, assert that `pool_map.total_tx_size` equals the sum of only the surviving entries (the 4th tx plus any non-evicted originals). Without the fix, the assertion fails because the stale pre-eviction local is written back. [9](#0-8)

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

**File:** tx-pool/src/component/pool_map.rs (L598-625)
```rust
        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
        }

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

**File:** tx-pool/src/component/pool_map.rs (L738-740)
```rust
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L742-749)
```rust
            _ => {
                if let Some((total_tx_size, total_tx_cycles)) = self.recompute_total_stat() {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, recomputed size {} cycles {}",
                        tx_size, cycles, total_tx_size, total_tx_cycles
                    );
                    self.total_tx_size = total_tx_size;
                    self.total_tx_cycles = total_tx_cycles;
```
