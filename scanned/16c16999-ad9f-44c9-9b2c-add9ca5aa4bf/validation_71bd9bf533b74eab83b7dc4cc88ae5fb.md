### Title
Stale `total_tx_size`/`total_tx_cycles` Overwrite After Eviction in `add_entry` Inflates Pool Accounting - (File: tx-pool/src/component/pool_map.rs)

---

### Summary

In `PoolMap::add_entry`, the new pool-wide `total_tx_size` and `total_tx_cycles` are computed **before** any evictions occur inside `check_and_record_ancestors`. When that call evicts existing entries (via `remove_entry` → `update_stat_for_remove_tx`), the in-place decrements to `self.total_tx_size`/`self.total_tx_cycles` are immediately **overwritten** by the stale pre-eviction values. The result is that the pool's size and cycle counters are permanently inflated by the size/cycles of every evicted transaction, causing false `Reject::Full` rejections and unnecessary evictions of legitimate transactions.

---

### Finding Description

`PoolMap::add_entry` executes the following sequence:

```rust
// Step 1 – compute new totals using CURRENT (pre-eviction) self.total_tx_size/cycles
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // line 210-211

// Step 2 – may evict entries; each eviction calls update_stat_for_remove_tx,
//           which immediately DECREMENTS self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;          // line 213

self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Step 3 – OVERWRITE with the stale pre-eviction values, discarding the decrements
self.total_tx_size = total_tx_size;                             // line 218
self.total_tx_cycles = total_tx_cycles;                         // line 219
``` [1](#0-0) 

`updated_stat_for_add_tx` snapshots `self.total_tx_size + entry.size` into a local variable before any mutation occurs: [2](#0-1) 

`remove_entry` (called transitively by `check_and_record_ancestors` when it evicts cell-dep consumers) immediately applies the decrement to `self.total_tx_size`/`self.total_tx_cycles` via `update_stat_for_remove_tx`: [3](#0-2) [4](#0-3) 

Because the local `total_tx_size`/`total_tx_cycles` were captured **before** those decrements, writing them back at lines 218-219 silently discards the eviction accounting. After the call, `self.total_tx_size` is inflated by exactly the sum of sizes of all evicted entries, and `self.total_tx_cycles` is inflated by their cycles.

The eviction path is documented in the code comment on `add_entry`: evictions occur when the incoming transaction **consumes a cell dep** that is already referenced by one or more pool entries. [5](#0-4) 

---

### Impact Explanation

**Inflated `total_tx_size` / `total_tx_cycles`** has two direct consequences:

1. **False `Reject::Full` for legitimate transactions.** `updated_stat_for_add_tx` uses `self.total_tx_size` to guard against pool overflow. If the counter is inflated, honest transactions are rejected even when the pool has physical room. [6](#0-5) 

2. **Unnecessary eviction of legitimate transactions.** `limit_size` loops while `self.pool_map.total_tx_size > self.config.max_tx_pool_size` and evicts the lowest-fee-rate entries. An inflated counter causes this loop to run extra iterations, evicting honest transactions that should have remained in the pool. [7](#0-6) 

Both effects are persistent: the inflation accumulates across multiple such insertions and is never corrected unless `recompute_total_stat` is triggered by an underflow (which only fires on removal, not on addition). [8](#0-7) 

---

### Likelihood Explanation

The trigger condition — a new transaction that **consumes a cell dep already referenced by pool entries** — is fully attacker-controlled. An unprivileged actor can:

1. Observe the mempool (via `get_raw_tx_pool` RPC) to identify live cell deps referenced by pending transactions.
2. Craft and submit (via `send_transaction` RPC or P2P relay) a transaction whose inputs consume one of those cell deps.
3. Each such submission inflates `total_tx_size` by the aggregate size of the evicted entries.

No privileged access, key material, or majority hashpower is required. The attack is repeatable and the inflation is cumulative.

---

### Recommendation

Compute `total_tx_size` and `total_tx_cycles` **after** all evictions have been applied, not before. The simplest fix is to move the `updated_stat_for_add_tx` call (or the final assignment) to after `check_and_record_ancestors` returns:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
// Now self.total_tx_size reflects post-eviction reality
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
// ... rest of the function unchanged
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, instead of caching the computed values in locals, update `self.total_tx_size`/`self.total_tx_cycles` in-place at the end using saturating arithmetic, mirroring the pattern already used in `update_stat_for_remove_tx`.

---

### Proof of Concept

**Setup:** Pool contains entry `A` (size=1000, cycles=1000) that references cell dep `D`. Pool `total_tx_size = 1000`.

**Attack step:** Attacker submits transaction `B` whose input consumes cell dep `D`.

**Execution trace inside `add_entry` for `B` (size=500, cycles=500):**

| Step | Action | `self.total_tx_size` |
|------|--------|----------------------|
| Before | — | 1000 |
| 1 | `updated_stat_for_add_tx(500, 500)` → local `total_tx_size = 1500` | 1000 (unchanged) |
| 2 | `check_and_record_ancestors` evicts `A`; `update_stat_for_remove_tx(1000,…)` runs | 0 |
| 3 | `self.total_tx_size = 1500` (stale local) | **1500** ← wrong |

**Expected:** `self.total_tx_size = 0 + 500 = 500` (only `B` remains).
**Actual:** `self.total_tx_size = 1500` — inflated by 1000 (the size of evicted `A`).

Subsequent honest transactions of size ≥ `max_tx_pool_size − 1500` are now falsely rejected with `Reject::Full`, or legitimate pool entries are unnecessarily evicted by `limit_size`. [1](#0-0) [4](#0-3)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L197-221)
```rust
    /// - succ  : means whether the entry is inserted actually into pool,
    /// - evicts: is the evicted transactions before inserting this `TxEntry`,
    ///   Currently, evicts when inserting is only due to referring cell dep will be consumed by this new transaction.
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

**File:** tx-pool/src/component/pool_map.rs (L235-250)
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
        })
    }
```

**File:** tx-pool/src/component/pool_map.rs (L698-708)
```rust
    fn recompute_total_stat(&self) -> Option<(usize, Cycle)> {
        self.entries.iter().try_fold(
            (0usize, 0 as Cycle),
            |(total_size, total_cycles), (_, entry)| {
                Some((
                    total_size.checked_add(entry.inner.size)?,
                    total_cycles.checked_add(entry.inner.cycles)?,
                ))
            },
        )
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

**File:** tx-pool/src/component/pool_map.rs (L731-758)
```rust
    /// Update size and cycles statistics for remove tx
    /// cycles overflow is possible, currently obtaining cycles is not accurate
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
