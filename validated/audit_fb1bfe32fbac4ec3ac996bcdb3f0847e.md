Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Inflated by Stale Snapshot Overwrite After Cell-Ref-Parent Eviction in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

`PoolMap::add_entry` snapshots `total_tx_size`/`total_tx_cycles` at lines 210–211 before calling `check_and_record_ancestors` at line 213, which may internally evict pool entries via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, correctly decrementing those counters. After eviction, lines 218–219 unconditionally overwrite `self.total_tx_size` and `self.total_tx_cycles` with the stale pre-eviction snapshot, permanently inflating the global accounting counters by the aggregate size/cycles of every evicted entry. Because `limit_size` drives all pool-size enforcement directly from `self.pool_map.total_tx_size`, an unprivileged submitter can repeatedly trigger this path to force the pool to appear full and evict honest transactions.

## Finding Description

In `add_entry` (lines 200–221 of `tx-pool/src/component/pool_map.rs`): [1](#0-0) 

The snapshot at lines 210–211 captures `self.total_tx_size + entry.size` before any eviction occurs. `check_and_record_ancestors` at line 213 enters the eviction branch when `ancestors_count > max_ancestors_count` and `ancestors_count.saturating_sub(cell_ref_parents.len()) <= max_ancestors_count` (line 603), calling `remove_entry_and_descendants` (line 618) → `remove_entry` (line 263) → `update_stat_for_remove_tx` (line 247), which correctly decrements `self.total_tx_size`. [2](#0-1) [3](#0-2) 

After those decrements, lines 218–219 unconditionally overwrite with the pre-eviction snapshot:

```
actual pool size   = original_total − evicted_sizes + entry.size   (correct)
self.total_tx_size = original_total + entry.size                    (inflated by evicted_sizes)
```

Each attack cycle permanently inflates `self.total_tx_size` by the aggregate size of evicted entries until the next `clear()` or node restart.

## Impact Explanation

`limit_size` drives all pool-size enforcement using `self.pool_map.total_tx_size` directly: [4](#0-3) 

An inflated counter causes `limit_size` to evict legitimate transactions even when real pool occupancy is well within `max_tx_pool_size` (default 180 MB). Repeated attack cycles accumulate inflation until the pool continuously rejects or evicts all honest transactions. This constitutes a **persistent tx-pool DoS** matching the **High** impact category: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs.*

## Likelihood Explanation

The eviction path requires three conditions, all achievable by an unprivileged `send_transaction` RPC caller with no special keys or roles:

1. A chain of `max_ancestors_count + 1` (default: 26) transactions already in the pool.
2. At least one pool transaction referencing one of the chain's outputs as a **cell dep** (`cell_ref_parents.len() ≥ 1`).
3. A new transaction consuming that output as an input, giving it `> max_ancestors_count` ancestors. [5](#0-4) 

The chain of 26 transactions can be reused across multiple attack cycles (until committed), and new `cell_ref_parent` transactions can be submitted cheaply each cycle. No Sybil attack, 51% hashpower, or privileged access is required.

## Recommendation

Compute the new totals **after** `check_and_record_ancestors` completes, using the already-updated `self.total_tx_size`/`self.total_tx_cycles` as the base:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Compute totals AFTER evictions have already adjusted self.total_tx_size/cycles
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, accumulate the evicted sizes during `check_and_record_ancestors` and subtract them from the pre-computed snapshot before assigning.

## Proof of Concept

**Setup (all submitted via `send_transaction` RPC by an unprivileged user):**

1. Submit a chain `T1 → T2 → … → T26` where each `Ti` spends the output of `T(i-1)`.
2. Submit transaction `B` that uses `T1`'s output (index 0) as a **cell dep** (not an input). `B` is now a `cell_ref_parent` for any future transaction spending `T1`'s output as an input.
3. Submit transaction `C` that spends `T26`'s output as an input (giving it 26 ancestors: `T1…T26`) and also spends `T1`'s output (index 0) as another input.

**Execution path in `add_entry` for `C`:**

- `updated_stat_for_add_tx` snapshots `total_tx_size_snapshot = self.total_tx_size + size(C)`.
- `check_and_record_ancestors` finds `ancestors_count = 27 > 25` and `cell_ref_parents = {B}`. Since `27 − 1 = 26 > 25` still fails, it evicts `B` via `remove_entry_and_descendants`, correctly decrementing `self.total_tx_size -= size(B)`. [6](#0-5) 

- Lines 218–219 overwrite: `self.total_tx_size = total_tx_size_snapshot` (ignoring the decrement).
- **Inflation = `size(B)`** per cycle.

4. `limit_size` now sees `total_tx_size > max_tx_pool_size` once inflation accumulates sufficiently and evicts honest transactions.
5. Repeat steps 1–4 with fresh transactions to accumulate inflation until the pool continuously rejects or evicts all honest transactions. [7](#0-6)

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

**File:** tx-pool/src/component/pool_map.rs (L244-248)
```rust
            self.remove_entry_edges(&entry.inner);
            self.remove_entry_links(id);
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
            entry.inner
```

**File:** tx-pool/src/component/pool_map.rs (L595-625)
```rust
        let mut ancestors_count = ancestors.len() + 1;
        let mut evicted = Default::default();

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
