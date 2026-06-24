Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Inflated by Stale Snapshot Overwrite After Evictions in `add_entry` — (`tx-pool/src/component/pool_map.rs`)

## Summary

`PoolMap::add_entry` calls `updated_stat_for_add_tx`, which is a read-only `&self` method that returns a pre-eviction snapshot `(self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles)` without mutating any fields. It then calls `check_and_record_ancestors`, which may evict pool entries via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, correctly subtracting evicted sizes in-place. After `check_and_record_ancestors` returns, `add_entry` unconditionally overwrites `self.total_tx_size` and `self.total_tx_cycles` with the stale pre-eviction snapshot, permanently inflating both counters by the aggregate size/cycles of all evicted transactions. An unprivileged attacker can repeatedly trigger this path to inflate `total_tx_size` until `limit_size` begins rejecting all new transactions with `Reject::Full`.

## Finding Description

**Root cause — `updated_stat_for_add_tx` is read-only:**

`updated_stat_for_add_tx` takes `&self` (immutable reference) and only computes and returns new values; it does not modify `self.total_tx_size` or `self.total_tx_cycles`: [1](#0-0) 

**Eviction path in `check_and_record_ancestors`:**

When `ancestors_count > max_ancestors_count` but the excess is attributable to `cell_ref_parents`, the lowest-fee `cell_ref_parents` are evicted via `remove_entry_and_descendants`: [2](#0-1) 

Each eviction calls `remove_entry`, which calls `update_stat_for_remove_tx`, directly mutating `self.total_tx_size` and `self.total_tx_cycles` in-place: [3](#0-2) 

`update_stat_for_remove_tx` correctly subtracts the evicted entry's size/cycles from the live fields: [4](#0-3) 

**The overwrite — stale snapshot discards correct in-place mutations:**

After `check_and_record_ancestors` returns with evictions applied, `add_entry` unconditionally overwrites the correctly-updated fields with the stale pre-eviction snapshot: [5](#0-4) 

**Arithmetic of inflation per attack cycle:**

Let `S` = `self.total_tx_size` before `add_entry`, `E` = aggregate size of evicted entries, `N` = size of new entry.

- `updated_stat_for_add_tx` returns snapshot = `S + N`
- `check_and_record_ancestors` evictions reduce `self.total_tx_size` to `S − E`
- Lines 218–219 overwrite: `self.total_tx_size = S + N`
- True pool size after insertion = `(S − E) + N`
- **Inflation per cycle = `E`** (aggregate size of evicted transactions)

Each attack cycle compounds this inflation additively.

**`limit_size` uses `total_tx_size` as its sole gate:** [6](#0-5) 

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

Once `total_tx_size` is inflated past `max_tx_pool_size` (default 180 MB), `limit_size` continuously evicts legitimate pending transactions and every subsequent `send_transaction` call returns `Reject::Full`. The pool appears full while actually being nearly empty. The inflation is permanent until a node restart or `clear()` is triggered, since no normal operation corrects the overwritten counter. An attacker targeting multiple nodes simultaneously can degrade the mempool across the network, preventing legitimate transactions from propagating.

## Likelihood Explanation

The exploit requires no special privileges. The `send_transaction` RPC is open to any peer or RPC caller. The attacker only needs to submit valid, fee-paying transactions. The eviction branch in `check_and_record_ancestors` is reachable with `max_ancestors_count = 25` (default) by submitting 26 transactions each using the same live cell as `cell_dep`, then one transaction spending that cell as an input. The attack is repeatable with low cost (only transaction fees), and each cycle permanently inflates the counter by the aggregate size of the evicted transactions.

## Recommendation

Move the stat update to **after** `check_and_record_ancestors` returns, so the final assignment adds `entry.size`/`entry.cycles` to the already-correctly-updated `self.total_tx_size`/`self.total_tx_cycles`. Remove the pre-eviction snapshot capture and instead perform a checked addition after all evictions are complete:

```rust
// In add_entry, replace:
//   let (total_tx_size, total_tx_cycles) = self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
//   ...
//   self.total_tx_size = total_tx_size;
//   self.total_tx_cycles = total_tx_cycles;

// With: keep the overflow check but don't capture the snapshot
self.check_stat_overflow_for_add_tx(entry.size, entry.cycles)?; // overflow guard only
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Add to post-eviction totals:
self.total_tx_size = self.total_tx_size.checked_add(entry.size).expect("overflow");
self.total_tx_cycles = self.total_tx_cycles.checked_add(entry.cycles).expect("overflow");
```

This ensures eviction subtractions performed by `update_stat_for_remove_tx` are not overwritten.

## Proof of Concept

**Setup**: Pool with `max_ancestors_count = 25`, `max_tx_pool_size = 180 MB`.

1. Submit 26 transactions `T1…T26`, each using cell `C` as a `cell_dep`. All are accepted. `total_tx_size ≈ 26 × 600 bytes = 15,600 bytes`.
2. Submit `T_spend` spending cell `C` as an input. Inside `add_entry`:
   - `updated_stat_for_add_tx` snapshots `total_tx_size_snapshot = 15,600 + size(T_spend)`.
   - `check_and_record_ancestors` finds 26 `cell_ref_parents`, evicts them. `update_stat_for_remove_tx` is called 26 times, reducing `self.total_tx_size` to `0`.
   - Lines 218–219 write `self.total_tx_size = 15,600 + size(T_spend)`.
   - **Actual pool**: only `T_spend`. **Reported size**: ~16,200 bytes (inflated by ~15,600 bytes).
3. Repeat steps 1–2 `N` times. After `N` iterations, `total_tx_size ≈ N × 15,600 bytes` while the pool is nearly empty.
4. After ~11,700 iterations (at 600 bytes/tx), `total_tx_size > 180 MB`. `limit_size` evicts all legitimate transactions and all new `send_transaction` calls return `Reject::Full`. [7](#0-6)

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

**File:** tx-pool/src/component/pool_map.rs (L235-248)
```rust
    pub(crate) fn remove_entry(&mut self, id: &ProposalShortId) -> Option<TxEntry> {
        self.entries.remove_by_id(id).map(|entry| {
            debug!(
                "remove entry {} from status: {:?}",
                entry.inner.transaction().hash(),
                entry.status
            );
            self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
            self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
            self.remove_entry_edges(&entry.inner);
            self.remove_entry_links(id);
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
            entry.inner
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

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```
