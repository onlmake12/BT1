### Title
Tx-Pool `total_tx_size`/`total_tx_cycles` Inflated When Cell-Ref Ancestor Eviction Occurs in `add_entry` — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the aggregate pool-size and pool-cycles counters (`total_tx_size`, `total_tx_cycles`) are pre-computed **before** `check_and_record_ancestors` is called. When that function evicts cell-ref-parent transactions to satisfy the ancestor-count limit, it correctly decrements the counters via `update_stat_for_remove_tx`. However, `add_entry` then **overwrites** those correctly-updated values with the stale pre-computed snapshot, permanently inflating both counters by the total size and cycles of every evicted entry. This is the direct analog of the reported "counter updated in some code paths but not others" class.

---

### Finding Description

`PoolMap::add_entry` (`tx-pool/src/component/pool_map.rs`, lines 200–221) executes the following sequence:

```
1. (L210-211) total_tx_size_new  = self.total_tx_size  + new_entry.size   // snapshot taken HERE
              total_tx_cycles_new = self.total_tx_cycles + new_entry.cycles

2. (L213)     evicts = self.check_and_record_ancestors(&mut entry)?
              // ↳ may call remove_entry_and_descendants()
              //   ↳ calls update_stat_for_remove_tx() for each evicted entry
              //     ↳ correctly decrements self.total_tx_size / self.total_tx_cycles

3. (L218-219) self.total_tx_size  = total_tx_size_new   // OVERWRITES the correctly-updated value
              self.total_tx_cycles = total_tx_cycles_new
```

`check_and_record_ancestors` (lines 603–625) evicts entries when:
- `ancestors_count > max_ancestors_count` (default 25 in production config), **and**
- `ancestors_count − cell_ref_parents.len() ≤ max_ancestors_count`

Under this condition it calls `remove_entry_and_descendants` (line 618), which calls `update_stat_for_remove_tx` (line 247) for every removed entry, correctly reducing `self.total_tx_size` and `self.total_tx_cycles`. But the stale snapshot assigned at lines 218–219 does not include those subtractions.

**Net effect after one such `add_entry` call:**

| Counter | Correct value | Actual value |
|---|---|---|
| `total_tx_size` | `old + new_size − evicted_sizes` | `old + new_size` |
| `total_tx_cycles` | `old + new_cycles − evicted_cycles` | `old + new_cycles` |

Each triggering call inflates the counters by `evicted_sizes` / `evicted_cycles`. The inflation is permanent and cumulative across repeated calls.

---

### Impact Explanation

`total_tx_size` is the sole gate for pool-full rejection and size-based eviction:

- **`limit_size`** (`pool.rs`, line 298) evicts entries while `self.pool_map.total_tx_size > self.config.max_tx_pool_size`. An inflated counter causes legitimate, already-admitted transactions to be evicted unnecessarily.
- **`updated_stat_for_add_tx`** (`pool_map.rs`, lines 716–728) rejects new submissions with `Reject::Full` when the inflated counter would overflow or exceed the pool limit. Valid transactions are denied entry.
- **`estimate_fee_rate`** uses the pool's entry set, not the counters directly, so fee estimation is unaffected; however, the pool appears artificially full to all admission logic.

The result is a **tx-pool denial-of-service**: an attacker can repeatedly trigger the eviction path to inflate the counters until the pool refuses all new transactions, even though the pool's actual byte occupancy is well below `max_tx_pool_size` (180 MB by default).

---

### Likelihood Explanation

The eviction path is reachable by any unprivileged `send_transaction` RPC caller. The attacker needs to:

1. Submit a chain of 25 transactions (the default `max_ancestors_count`) linked by inputs.
2. Submit one of those transactions as a **cell dep** of a new transaction (making it a `cell_ref_parent`).
3. Submit the new transaction whose ancestor count is 26 (> 25) but drops to 25 when the cell-ref parent is evicted.

This satisfies the condition at line 603 and triggers the eviction loop at line 616–625. The attacker pays only normal transaction fees. The attack can be repeated in a loop to accumulate inflation. No privileged access, no majority hashpower, and no Sybil attack is required.

---

### Recommendation

Move the `total_tx_size` / `total_tx_cycles` assignment to **after** `check_and_record_ancestors` returns, and compute the final values from the **post-eviction** state of `self`:

```rust
// In add_entry, replace lines 210-211 and 218-219 with:
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Compute totals AFTER evictions have already updated self.total_tx_size/cycles
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size  = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, subtract the evicted entries' sizes/cycles from the pre-computed snapshot before assigning.

---

### Proof of Concept

**Setup** (default `max_ancestors_count = 25`):

1. Submit transactions `T0 → T1 → … → T24` (a 25-deep input chain). All are admitted; `total_tx_size = 25 * S`.
2. Submit transaction `T25` whose inputs spend `T24`'s output **and** whose `cell_deps` reference `T0`'s output. Now `T25` has 26 ancestors (T0…T24 + T0 as cell-ref parent). `ancestors_count (26) > max_ancestors_count (25)` and `26 − 1 = 25 ≤ 25`, so the eviction branch fires.
3. `check_and_record_ancestors` calls `remove_entry_and_descendants(T0)`, which removes T0 and all its descendants (T1…T24 — 25 entries total, size `25 * S`). `self.total_tx_size` is correctly decremented to `0`.
4. Back in `add_entry`, line 218 assigns `total_tx_size = 25*S + S_T25` (the pre-computed snapshot), overwriting `0 + S_T25`.
5. **Result**: `total_tx_size` is now `25*S + S_T25` instead of `S_T25`. The pool believes it holds 26 transactions worth of bytes when it holds only 1.
6. Repeat from step 1 to accumulate inflation until `total_tx_size ≥ max_tx_pool_size (180 MB)`, at which point all subsequent `send_transaction` calls are rejected with `Reject::Full`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** tx-pool/src/pool.rs (L292-328)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
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
        ret
```

**File:** resource/ckb.toml (L210-216)
```text
[tx_pool]
max_tx_pool_size = 180_000_000 # 180mb
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
max_tx_verify_cycles = 70_000_000
max_ancestors_count = 25
```
