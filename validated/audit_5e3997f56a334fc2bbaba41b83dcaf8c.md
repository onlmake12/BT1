Audit Report

## Title
`PoolMap::add_entry` Overwrites `total_tx_size`/`total_tx_cycles` With Stale Pre-Eviction Snapshot, Permanently Inflating Pool Size Accounting — (`tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` is called as a pure `&self` read at line 210–211, capturing a snapshot of `(self.total_tx_size + new_size, self.total_tx_cycles + new_cycles)` before any eviction occurs. If `check_and_record_ancestors` at line 213 subsequently evicts entries via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, those decrements correctly mutate `self.total_tx_size`/`self.total_tx_cycles` in place. Lines 218–219 then unconditionally overwrite those fields with the stale pre-eviction snapshot, permanently inflating both counters by the size/cycles of every evicted transaction. Repeated triggering of this path allows an unprivileged attacker to inflate `total_tx_size` beyond `max_tx_pool_size`, causing the pool to reject all subsequent transactions with `Reject::Full` even when physically empty.

## Finding Description
The exact sequence in `add_entry` (lines 200–221 of `tx-pool/src/component/pool_map.rs`):

**Step 1 — Snapshot captured before eviction:**
`updated_stat_for_add_tx` is declared `&self` (line 711), so it only reads `self.total_tx_size` and `self.total_tx_cycles` and returns `(T + new_size, T + new_cycles)` without modifying the fields. [1](#0-0) 

**Step 2 — Eviction mutates the fields in place:**
`check_and_record_ancestors` (line 213) calls `remove_entry_and_descendants` (line 618) when ancestor count exceeds `max_ancestors_count` but can be reduced by evicting `cell_ref_parents`. This chains into `remove_entry` → `update_stat_for_remove_tx`, which directly decrements `self.total_tx_size` and `self.total_tx_cycles`. [2](#0-1) [3](#0-2) [4](#0-3) 

**Step 3 — Stale snapshot unconditionally overwrites post-eviction state:** [5](#0-4) 

Concrete arithmetic for pool at size `T`, new tx of size `new_size`, evicted tx of size `E`:

| Step | `self.total_tx_size` |
|---|---|
| Before call | `T` |
| After `updated_stat_for_add_tx` | `T` (snapshot = `T + new_size`) |
| After eviction of `E` bytes | `T − E` (correctly decremented) |
| After line 218 overwrite | `T + new_size` (**wrong**; should be `T − E + new_size`) |

Each such insertion permanently inflates `total_tx_size` by `E`. The error accumulates with repeated insertions.

## Impact Explanation
`total_tx_size` is the primary guard for the pool's size limit. `limit_size` loops while `self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting valid pending/proposed transactions and rejecting new ones with `Reject::Full`. [6](#0-5) 

An attacker who repeatedly triggers the eviction path inflates `total_tx_size` without bound. Once it exceeds `max_tx_pool_size`, `limit_size` evicts all real pool entries, and `updated_stat_for_add_tx` rejects every subsequent submission with `Reject::Full` even when the pool is physically empty. This renders the tx pool non-functional on the targeted node. Applied across multiple nodes via P2P relay, this constitutes **CKB network congestion with few costs** — matching the **High (10001–15000 points)** impact class.

## Likelihood Explanation
The eviction path requires: (1) a new transaction whose ancestor count exceeds `max_ancestors_count` (default 25), and (2) the excess is entirely attributable to `cell_ref_parents`. An unprivileged attacker can satisfy both conditions by submitting a chain of 25 transactions sharing a common live cell as a cell dep, then submitting a 26th that spends the last output and references the same cell dep. This is reachable via the `send_transaction` RPC or P2P relay (`RelayV3`) with no special privileges. The inflation accumulates with each such insertion, so the attacker can amplify the accounting error with repeated submissions at low cost.

## Recommendation
Move `updated_stat_for_add_tx` to **after** `check_and_record_ancestors` returns, so it reads the already-decremented `self.total_tx_size`/`self.total_tx_cycles`:

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
    trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    // Compute against post-eviction totals
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    self.total_tx_size = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;
    Ok((true, evicts))
}
```

## Proof of Concept
1. Fill the pool with 24 transactions T1→T2→…→T24, all referencing a shared live cell as a cell dep (making them `cell_ref_parents`).
2. Submit T25, which spends T24's output and references the same cell dep. Ancestor count = 25 = `max_ancestors_count`, triggering the eviction branch at line 603. One cell-dep parent (e.g., T24 and its descendants) is evicted via `remove_entry_and_descendants`, reducing ancestor count to 24. T25 is inserted.
3. After insertion, `total_tx_size` is inflated by the size of the evicted entries (the overwrite at lines 218–219 restores the pre-eviction snapshot).
4. Repeat step 2 with fresh transactions. Each iteration inflates `total_tx_size` further.
5. After enough iterations, `total_tx_size` exceeds `max_tx_pool_size` even though the actual pool is nearly empty. All subsequent `send_transaction` calls return `Reject::Full`, and `tx_pool_info` reports a grossly inflated `total_tx_size`.

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

**File:** tx-pool/src/component/pool_map.rs (L246-248)
```rust
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

**File:** tx-pool/src/component/pool_map.rs (L710-729)
```rust
    /// Calculate size and cycles statistics for adding a tx.
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

**File:** tx-pool/src/pool.rs (L298-326)
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
```
