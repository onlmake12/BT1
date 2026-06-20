### Title
`PoolMap::add_entry` Stale Pre-Eviction Snapshot Overwrites Correctly-Updated `total_tx_size`/`total_tx_cycles`, Permanently Inflating Pool Size Accounting — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the pool's aggregate size and cycles statistics are snapshotted **before** ancestor-eviction occurs, then unconditionally written back **after** evictions, silently discarding the size/cycles reductions that `remove_entry_and_descendants` correctly applied. The result is that `total_tx_size` and `total_tx_cycles` grow monotonically whenever the cell-dep ancestor eviction path fires, permanently inflating the pool's self-reported occupancy. An unprivileged tx-pool submitter can exploit this to make the pool believe it is full, causing all subsequent transaction submissions to be rejected with `Reject::Full`.

---

### Finding Description

`PoolMap::add_entry` executes the following sequence: [1](#0-0) 

```
Step 1 (line 210-211): snapshot = self.total_tx_size + entry.size
Step 2 (line 213):     check_and_record_ancestors() → may call remove_entry_and_descendants()
                         → each removal calls update_stat_for_remove_tx()
                         → correctly decrements self.total_tx_size by evicted sizes
Step 3 (line 218-219): self.total_tx_size = snapshot   ← OVERWRITES the corrected value
                        self.total_tx_cycles = snapshot  ← OVERWRITES the corrected value
```

The eviction path inside `check_and_record_ancestors` fires when the total ancestor count (including cell-dep ancestors) exceeds `max_ancestors_count`, but removing cell-dep parents would bring it back under the limit: [2](#0-1) 

Each eviction calls `remove_entry_and_descendants`, which calls `remove_entry`, which calls `update_stat_for_remove_tx`: [3](#0-2) 

`update_stat_for_remove_tx` correctly decrements `self.total_tx_size` and `self.total_tx_cycles`: [4](#0-3) 

But immediately after, lines 218-219 overwrite `self.total_tx_size` and `self.total_tx_cycles` with the stale pre-eviction snapshot: [5](#0-4) 

**Net arithmetic effect per eviction event:**

```
Expected: total_tx_size = original - evicted_sizes + entry.size
Actual:   total_tx_size = original + entry.size
Error:    +evicted_sizes (permanently leaked into the counter)
```

The `total_tx_size` field is the sole guard for pool admission in `limit_size`: [6](#0-5) 

And for the `is_full` check in `VerifyQueue`: [7](#0-6) 

---

### Impact Explanation

Every time the cell-dep ancestor eviction path fires during `add_entry`, `total_tx_size` is inflated by the sum of sizes of all evicted entries. This inflation is permanent — it is never corrected unless the pool is fully cleared. Consequences:

1. **Spurious `Reject::Full` rejections**: Legitimate transactions are rejected even when the pool has actual capacity, because `total_tx_size` exceeds `max_tx_pool_size` on paper.
2. **Unnecessary evictions in `limit_size`**: The pool evicts valid pending/proposed transactions to bring the inflated counter below the limit, discarding user transactions that should have been kept.
3. **Monotonic degradation**: Each successful exploitation increases the inflation, making the pool progressively less usable until restarted.

---

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged tx-pool submitter. The attacker needs to:

1. Submit a set of transactions T₁…Tₙ where some Tᵢ are referenced as **cell deps** (not inputs) by others, forming a cell-dep ancestor chain.
2. Submit a new transaction Tₙₑw whose total ancestor count (via cell deps) exceeds `max_ancestors_count`, but whose non-cell-dep ancestor count is within the limit.
3. The eviction path fires, removing the cell-dep parents and their descendants.
4. `total_tx_size` is inflated by the removed entries' sizes.
5. Repeat until `total_tx_size > max_tx_pool_size`.

This requires no privileged access, no keys, and no majority hashpower. It is a standard tx-pool submission attack reachable via the `send_transaction` RPC or the P2P relay protocol.

---

### Recommendation

Move the `updated_stat_for_add_tx` call to **after** `check_and_record_ancestors` completes, so the snapshot is taken from the already-eviction-corrected `self.total_tx_size`:

```rust
pub(crate) fn add_entry(
    &mut self,
    mut entry: TxEntry,
    status: Status,
) -> Result<(bool, HashSet<TxEntry>), Reject> {
    let tx_short_id = entry.proposal_short_id();
    if self.entries.get_by_id(&tx_short_id).is_some() {
        return Ok((false, Default::default()));
    }
    // Perform evictions first, THEN snapshot the updated totals
    let evicts = self.check_and_record_ancestors(&mut entry)?;
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    self.total_tx_size = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;
    Ok((true, evicts))
}
```

---

### Proof of Concept

```
1. Pool starts empty: total_tx_size = 0, max_tx_pool_size = M

2. Attacker submits T_dep (size=S_dep) as a cell-dep provider.
   Pool: total_tx_size = S_dep

3. Attacker submits T_chain[1..N] each referencing T_dep as a cell dep,
   building a cell-dep ancestor chain of depth N > max_ancestors_count.

4. Attacker submits T_new referencing T_chain[1..N] as cell deps.
   - add_entry is called for T_new
   - Line 210: snapshot = total_tx_size + size(T_new)
                         = (S_dep + N*S_chain) + S_new
   - check_and_record_ancestors fires the eviction path:
       removes T_dep and T_chain[1..N] (total evicted size = S_dep + N*S_chain)
       update_stat_for_remove_tx correctly sets:
         self.total_tx_size = 0
   - Line 218: self.total_tx_size = snapshot = S_dep + N*S_chain + S_new
               (evicted sizes are re-added, pool thinks it has S_dep + N*S_chain extra bytes)

5. Repeat steps 2-4 until total_tx_size > M.

6. All subsequent send_transaction calls return Reject::Full.
   Legitimate users cannot submit transactions.
``` [8](#0-7) [9](#0-8) [10](#0-9)

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

**File:** tx-pool/src/component/pool_map.rs (L246-247)
```rust
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
```

**File:** tx-pool/src/component/pool_map.rs (L588-640)
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
        } else {
            return Err(Reject::ExceededMaximumAncestorsCount);
        }

        // some txs in `parents` are removed, now `ancestors` need to re-caculate,
        let ancestors = self
            .links
            .calc_relation_ids(parents.clone(), Relation::Parents);

        // we can assume the number now is less than `max_ancestors_count`
        assert!(ancestors.len() < self.max_ancestors_count);

        self._record_ancestors(entry, ancestors, parents);
        Ok(evicted)
    }
```

**File:** tx-pool/src/component/pool_map.rs (L668-684)
```rust
    fn track_entry_statics(&mut self, remove: Option<Status>, add: Option<Status>) {
        match remove {
            Some(Status::Pending) => self.pending_count -= 1,
            Some(Status::Gap) => self.gap_count -= 1,
            Some(Status::Proposed) => self.proposed_count -= 1,
            _ => {}
        }
        match add {
            Some(Status::Pending) => self.pending_count += 1,
            Some(Status::Gap) => self.gap_count += 1,
            Some(Status::Proposed) => self.proposed_count += 1,
            _ => {}
        }
        assert_eq!(
            self.pending_count + self.gap_count + self.proposed_count,
            self.entries.len()
        );
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

**File:** tx-pool/src/component/verify_queue.rs (L104-106)
```rust
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
    }
```
