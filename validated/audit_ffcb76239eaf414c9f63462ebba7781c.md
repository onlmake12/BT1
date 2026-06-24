Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Overcounted When Evictions Occur During `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, new aggregate totals for `total_tx_size` and `total_tx_cycles` are captured as local variables before `check_and_record_ancestors` runs. When that function evicts transactions via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, it correctly decrements `self.total_tx_size`/`self.total_tx_cycles`. The final write-back at lines 218–219 then overwrites those correctly-decremented fields with the stale pre-eviction snapshot, permanently inflating both counters by the total size/cycles of every evicted transaction. An unprivileged attacker can repeat this to drive `total_tx_size` past `max_tx_pool_size`, causing the node to reject all subsequent `send_transaction` calls with `Reject::Full`.

## Finding Description

**Root cause — `add_entry` (lines 200–221):**

`updated_stat_for_add_tx` takes `&self` and returns `(self.total_tx_size + tx_size, self.total_tx_cycles + cycles)` without modifying any fields. [1](#0-0) 

`check_and_record_ancestors` is then called. When `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`, it enters the eviction branch and calls `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which **does** write the decremented values back to `self.total_tx_size`/`self.total_tx_cycles`. [2](#0-1) [3](#0-2) [4](#0-3) 

The stale local variables are then unconditionally written back, overwriting the correctly-decremented fields: [5](#0-4) 

**Concrete arithmetic:**

| Step | `self.total_tx_size` | local `total_tx_size` |
|---|---|---|
| Initial | X | — |
| After `updated_stat_for_add_tx` | X (unchanged) | X + new_size |
| After evicting E bytes | X − E (correct) | X + new_size (stale) |
| After line 218 | **X + new_size (wrong)** | — |

Correct value: `X − E + new_size`. Counter inflated by `E` per eviction event.

`recompute_total_stat` (lines 698–708) is only invoked on underflow during removal, not on this overcount path. [6](#0-5) 

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node (tx-pool DoS).**

`limit_size` loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting legitimate pending transactions based on the inflated counter. [7](#0-6) 

Once the inflated `total_tx_size` permanently exceeds `max_tx_pool_size`, every subsequent `send_transaction` call triggers `limit_size` and evicts real transactions, or `updated_stat_for_add_tx` returns `Reject::Full` before insertion. The node's mempool becomes permanently non-functional for accepting new transactions without a restart, constituting an effective crash of a critical node component reachable by any peer.

## Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged RPC caller via `send_transaction`. No key material, special privilege, or majority hash power is required. The attacker only needs to construct transactions that share a cell dep output, then consume that output — a standard transaction pattern. The inflation is cumulative and permanent (until node restart), so the attack succeeds with a small, bounded number of submissions. It is fully repeatable.

## Recommendation

Compute the new totals **after** `check_and_record_ancestors` completes, so evictions are already reflected in `self.total_tx_size`/`self.total_tx_cycles` before the new entry's contribution is added:

```rust
// Pre-check for overflow only; do NOT capture the new totals yet.
self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Add new entry's contribution AFTER evictions have already updated the fields.
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
Ok((true, evicts))
```

## Proof of Concept

1. Configure a node with default `max_ancestors_count = 25`.
2. Submit root transaction `T0` with one output `O0`.
3. Submit 26 transactions `C1…C26`, each with an independent input but cell-depping on `O0`. All are accepted (each has 1 ancestor: itself). Record `pool_info.total_tx_size = S` via RPC.
4. Submit transaction `T_consume` spending `O0` as an input. This triggers `check_and_record_ancestors`: `ancestors_count = 27 > 25`, all excess are `cell_ref_parents`. Two transactions (e.g., `C1`, `C2`) are evicted via `remove_entry_and_descendants`, correctly decrementing `self.total_tx_size` by `size(C1) + size(C2)`. Line 218 then overwrites with the stale snapshot. [8](#0-7) 
5. Observe via `tx_pool_info` RPC that `total_tx_size = S + size(T_consume)` instead of the correct `S - size(C1) - size(C2) + size(T_consume)`.
6. Repeat steps 3–5. Each iteration inflates `total_tx_size` by `size(C1) + size(C2)`.
7. Once `total_tx_size` exceeds `max_tx_pool_size`, all subsequent `send_transaction` calls return `Reject::Full` even though the pool has ample real space, confirmed by `recompute_total_stat` returning a value well below the limit. [9](#0-8)

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

**File:** tx-pool/src/component/pool_map.rs (L244-247)
```rust
            self.remove_entry_edges(&entry.inner);
            self.remove_entry_links(id);
            self.track_entry_statics(Some(entry.status), None);
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

**File:** tx-pool/src/component/pool_map.rs (L733-740)
```rust
    fn update_stat_for_remove_tx(&mut self, tx_size: usize, cycles: Cycle) {
        match (
            self.total_tx_size.checked_sub(tx_size),
            self.total_tx_cycles.checked_sub(cycles),
        ) {
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```
