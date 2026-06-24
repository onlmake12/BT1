Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Inflated by Stale Snapshot Overwrite After Evictions in `add_entry` — (`tx-pool/src/component/pool_map.rs`)

## Summary

`PoolMap::add_entry` captures a pre-eviction snapshot of `total_tx_size` and `total_tx_cycles` via `updated_stat_for_add_tx`, then calls `check_and_record_ancestors`, which may evict pool entries and correctly subtract their sizes in-place via `update_stat_for_remove_tx`. After `check_and_record_ancestors` returns, `add_entry` unconditionally overwrites `self.total_tx_size` and `self.total_tx_cycles` with the stale pre-eviction snapshot, permanently inflating both counters by the aggregate size/cycles of all evicted transactions. An unprivileged attacker can repeatedly trigger this path to inflate `total_tx_size` until `limit_size` begins evicting legitimate transactions and all new `send_transaction` calls return `Reject::Full`, even though the pool has real capacity.

## Finding Description

In `add_entry` (lines 200–221), the sequence is:

1. **Line 210–211**: `updated_stat_for_add_tx(entry.size, entry.cycles)` computes `total_tx_size_snapshot = self.total_tx_size + entry.size` and stores it in a local variable. This is a read-only snapshot; `self.total_tx_size` is not yet modified. [1](#0-0) 

2. **Line 213**: `check_and_record_ancestors` may call `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which **directly mutates** `self.total_tx_size` and `self.total_tx_cycles` in-place for each evicted entry. [2](#0-1) [3](#0-2) [4](#0-3) 

3. **Lines 218–219**: `self.total_tx_size = total_tx_size` and `self.total_tx_cycles = total_tx_cycles` overwrite the correctly-updated in-place values with the stale pre-eviction snapshot. [5](#0-4) 

The eviction path in `check_and_record_ancestors` is triggered when the incoming transaction has more than `max_ancestors_count` ancestors, but the excess is due to `cell_ref_parents` (transactions sharing a cell dep that the new tx spends as an input). In that case, the lowest-fee `cell_ref_parents` are evicted via `remove_entry_and_descendants`. [6](#0-5) 

After evictions, `self.total_tx_size` correctly equals `S − evict_size`. But the overwrite at line 218 sets it to `S + entry.size`, inflating it by `evict_size + entry.size` relative to the true pool contents. Each attack cycle compounds this inflation.

The inflated counter is the sole gate in `limit_size`: [7](#0-6) 

And is exposed directly via RPC in `TxPoolInfo`: [8](#0-7) 

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

Once `total_tx_size` is inflated past `max_tx_pool_size` (default 180 MB), `limit_size` continuously evicts legitimate pending transactions and every subsequent `send_transaction` call returns `Reject::Full`. The attack is cheap: it requires only valid, fee-paying transactions submitted via the open `send_transaction` RPC. An attacker targeting multiple nodes simultaneously can degrade the mempool across the network, preventing legitimate transactions from propagating and causing effective network congestion. The inflation persists until a node restart or a reorg triggers `clear()`, since no normal operation corrects the overwritten counter.

## Likelihood Explanation

The exploit requires no special privileges. Any peer or RPC caller can:
1. Submit ≥26 transactions each using the same live cell as `cell_dep` (valid, no privilege required).
2. Submit one transaction spending that cell as an input, triggering the eviction branch in `check_and_record_ancestors` and the overwrite in `add_entry`.
3. Repeat.

The `send_transaction` RPC is open to any node operator or relay peer. The attack is repeatable with low cost (only transaction fees), and each cycle permanently inflates the counter by the aggregate size of the evicted transactions.

## Recommendation

Move the stat update to **after** `check_and_record_ancestors` returns, so the final assignment adds `entry.size`/`entry.cycles` to the already-correctly-updated `self.total_tx_size`/`self.total_tx_cycles`:

```rust
// Remove the pre-eviction snapshot assignment:
// let (total_tx_size, total_tx_cycles) = self.updated_stat_for_add_tx(...)?;

// Keep the capacity check (overflow guard) but don't capture the snapshot:
self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Add entry.size/cycles to the post-eviction totals:
self.total_tx_size = self.total_tx_size.checked_add(entry.size).expect("overflow");
self.total_tx_cycles = self.total_tx_cycles.checked_add(entry.cycles).expect("overflow");
```

This ensures eviction subtractions performed by `update_stat_for_remove_tx` are not overwritten. [9](#0-8) 

## Proof of Concept

**Setup**: Pool with `max_ancestors_count = 25`, `max_tx_pool_size = 180 MB`.

1. Submit 26 transactions `T1…T26`, each using cell `C` as a `cell_dep`. All are accepted. `total_tx_size` = 26 × ~600 bytes = ~15,600 bytes.
2. Submit `T_spend` that spends cell `C` as an input. Inside `add_entry`:
   - `updated_stat_for_add_tx` snapshots `total_tx_size_snapshot = 15,600 + size(T_spend)`.
   - `check_and_record_ancestors` finds 26 `cell_ref_parents`, evicts them. `update_stat_for_remove_tx` is called 26 times, reducing `self.total_tx_size` to 0.
   - Line 218 writes `self.total_tx_size = 15,600 + size(T_spend)`.
   - **Actual pool**: only `T_spend`. **Reported size**: ~15,600 bytes inflated.
3. Repeat steps 1–2 `N` times. After `N` iterations, `total_tx_size ≈ N × 15,600` bytes while the pool is nearly empty.
4. Once `total_tx_size > 180 MB` (~11,700 iterations at 600 bytes/tx), `limit_size` evicts all legitimate transactions and all new `send_transaction` calls return `Reject::Full`. [10](#0-9) [11](#0-10)

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

**File:** tx-pool/src/component/pool_map.rs (L738-740)
```rust
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
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

**File:** tx-pool/src/service.rs (L1089-1089)
```rust
            total_tx_size: tx_pool.pool_map.total_tx_size,
```
