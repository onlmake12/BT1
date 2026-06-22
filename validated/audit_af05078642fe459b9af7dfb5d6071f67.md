I found the vulnerability. Let me verify the exact code flow before writing the report.

### Title
Stale-Snapshot Overwrite in `add_entry` Inflates `total_tx_size`/`total_tx_cycles`, Enabling Tx-Pool DoS — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::add_entry` snapshots the projected pool totals before calling `check_and_record_ancestors`, which may evict existing entries and correctly decrement `self.total_tx_size`/`self.total_tx_cycles` via `update_stat_for_remove_tx`. Immediately after, the function unconditionally overwrites those fields with the pre-eviction snapshot, re-inflating the totals by the size and cycles of every evicted transaction. An unprivileged RPC caller can craft a transaction chain that repeatedly triggers this path, permanently inflating the pool's accounting until the pool appears full and rejects all new transactions.

---

### Finding Description

**Root cause — `add_entry` in `pool_map.rs` lines 200–221:**

```
pub(crate) fn add_entry(…) -> Result<(bool, HashSet<TxEntry>), Reject> {
    …
    // STEP 1 – snapshot: total_tx_size = current + new_tx_size
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // lines 210-211

    // STEP 2 – may evict entries; each eviction calls update_stat_for_remove_tx,
    //           which correctly decrements self.total_tx_size / self.total_tx_cycles
    evicts = self.check_and_record_ancestors(&mut entry)?;          // line 213

    …
    // STEP 3 – OVERWRITES the correctly-decremented fields with the stale snapshot
    self.total_tx_size  = total_tx_size;                            // line 218
    self.total_tx_cycles = total_tx_cycles;                         // line 219
    Ok((true, evicts))
}
```

**Eviction path inside `check_and_record_ancestors` (lines 603–625):**

When `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`, the function evicts `cell_ref_parents` one by one:

```
let removed = self.remove_entry_and_descendants(next_id);   // line 618
```

`remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx` (line 247 / lines 733–740) correctly subtracts each evicted entry's `size` and `cycles` from `self.total_tx_size` / `self.total_tx_cycles`.

**The accounting error:**

Let `S` = pool size before the call, `E` = total size of evicted entries, `N` = new tx size.

| Step | `self.total_tx_size` |
|---|---|
| After `updated_stat_for_add_tx` snapshot | `S + N` (stored in local `total_tx_size`) |
| After evictions inside `check_and_record_ancestors` | `S - E` (correct) |
| After overwrite at line 218 | `S + N` (stale snapshot, **E is lost**) |

Correct value should be `S - E + N`. Actual value is `S + N`. The pool's accounting is inflated by `E` per triggering call.

**Trigger condition (reachable without privilege):**

`cell_ref_parents` are pool transactions whose outputs are referenced as cell deps by the inputs of the new transaction. This is a standard CKB pattern: a transaction can reference another unconfirmed transaction's output as a cell dep. An attacker builds a transaction graph where:

1. A set of "dep-provider" transactions (`D1…Dk`) are submitted to the pool.
2. A chain of 25+ ancestor transactions is built, some of which spend outputs that `D1…Dk` also reference as cell deps, making `D1…Dk` become `cell_ref_parents` of the new transaction.
3. The new transaction is submitted via `send_transaction` RPC. `ancestors_count` exceeds `max_ancestors_count` (default 25), but `ancestors_count - k <= 25`, so the eviction branch fires.
4. `D1…Dk` are evicted, their sizes are subtracted from `self.total_tx_size`, but the stale snapshot immediately restores the inflated value.
5. Repeating this inflates `total_tx_size` by `sum(size(Di))` per round.

---

### Impact Explanation

`limit_size` (pool.rs line 298) evicts legitimate pending transactions whenever `self.pool_map.total_tx_size > self.config.max_tx_pool_size` (default 180 MB). With `total_tx_size` artificially inflated:

- Legitimate transactions are evicted from the pool even though actual memory usage is well within the limit.
- New transactions submitted by honest users are rejected with `Reject::Full` even when the pool has ample space.
- The pool's `total_tx_cycles` is similarly inflated, distorting fee-rate estimation returned to miners and RPC callers.
- Repeated exploitation drives `total_tx_size` to `max_tx_pool_size`, after which the pool permanently rejects all incoming transactions — a complete mempool denial-of-service.

The only recovery is a node restart (which resets in-memory pool state).

---

### Likelihood Explanation

The trigger requires constructing a transaction graph where a new transaction has more than `max_ancestors_count` ancestors and some of those ancestors are `cell_ref_parents`. This is a valid, protocol-permitted pattern in CKB (cell deps referencing unconfirmed outputs). No privileged access, no key material, and no majority hashpower is required. The attack is executable by any RPC-accessible user (including remote callers if the RPC port is exposed, or local callers on a public node). The default `max_ancestors_count` of 25 is low enough that the required chain depth is easily achievable.

---

### Recommendation

Move the stat snapshot **after** all evictions have completed, so it reflects the post-eviction pool state:

```rust
pub(crate) fn add_entry(…) -> Result<(bool, HashSet<TxEntry>), Reject> {
    …
    // Validate capacity before evictions (overflow check only)
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));

    // Recompute AFTER evictions so evicted sizes are not re-added
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    self.total_tx_size  = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;
    Ok((true, evicts))
}
```

Alternatively, remove the local snapshot entirely and apply the increment directly after all mutations, or use `update_stat_for_remove_tx` / a paired increment rather than a pre-computed overwrite.

---

### Proof of Concept

**Setup:**

- `max_ancestors_count = 25` (default).
- Submit 24 ancestor transactions `A1 → A2 → … → A24` (each spending the previous output).
- Submit a "dep-provider" transaction `D` whose output `D:0` is referenced as a cell dep by `A1`.
- Submit a new transaction `T` that spends `A24`'s output and also references `D:0` as a cell dep input (making `D` a `cell_ref_parent` of `T`).

**Execution trace:**

1. `add_entry(T)` is called.
2. `updated_stat_for_add_tx` snapshots `total_tx_size = S + size(T)`.
3. `check_and_record_ancestors`: `ancestors_count = 25 + 1 = 26 > 25`; `cell_ref_parents = {D}`; `26 - 1 = 25 <= 25` → eviction branch fires.
4. `remove_entry_and_descendants(D)` → `update_stat_for_remove_tx(size(D), cycles(D))` → `self.total_tx_size = S - size(D)`.
5. Back in `add_entry`: `self.total_tx_size = total_tx_size` → `self.total_tx_size = S + size(T)`.
6. Correct value: `S - size(D) + size(T)`. Actual value: `S + size(T)`. **Inflation: `size(D)`.**

Repeating with fresh `D` transactions each round accumulates inflation until `total_tx_size >= max_tx_pool_size`, at which point `limit_size` evicts all pending transactions and the pool rejects every subsequent submission with `Reject::Full`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** tx-pool/src/component/pool_map.rs (L588-625)
```rust
    fn check_and_record_ancestors(
        &mut self,
        entry: &mut TxEntry,
    ) -> Result<HashSet<TxEntry>, Reject> {
        let tx = entry.transaction();
        let (ancestors, mut parents, cell_ref_parents) = self.get_tx_ancenstors(tx);

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

**File:** tx-pool/src/component/pool_map.rs (L710-758)
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
