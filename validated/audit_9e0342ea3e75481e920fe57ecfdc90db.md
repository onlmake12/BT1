### Title
Tx-Pool `total_tx_size`/`total_tx_cycles` Overstated When Eviction Occurs During Entry Insertion — (`tx-pool/src/component/pool_map.rs`)

### Summary

In `PoolMap::add_entry`, the aggregate accounting fields `total_tx_size` and `total_tx_cycles` are pre-computed before `check_and_record_ancestors` runs. When that function evicts conflicting transactions (via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`), it correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in-place. However, at the end of `add_entry`, the stale pre-computed values are unconditionally written back, silently overwriting the eviction-adjusted values. The result is that `total_tx_size` and `total_tx_cycles` are permanently overstated by the sum of all evicted transactions' sizes and cycles, causing key pool-admission and pool-eviction logic to malfunction.

### Finding Description

In `add_entry`, the sequence is:

1. Pre-compute new totals **before** any eviction: [1](#0-0) 

2. Call `check_and_record_ancestors`, which may evict transactions and **mutate** `self.total_tx_size` / `self.total_tx_cycles` via `update_stat_for_remove_tx`: [2](#0-1) [3](#0-2) 

3. Unconditionally overwrite `self.total_tx_size` and `self.total_tx_cycles` with the stale pre-eviction values: [4](#0-3) 

The eviction path inside `check_and_record_ancestors` is triggered when a new transaction's ancestor count exceeds `max_ancestors_count` but can be reduced to within the limit by evicting "cell-ref-parent" transactions (those that use the same cell as a dep): [5](#0-4) 

Each evicted entry correctly calls `update_stat_for_remove_tx` through `remove_entry`: [6](#0-5) 

But those decrements are immediately discarded when the stale locals are written back at lines 218–219.

The `update_stat_for_remove_tx` function itself even carries a comment acknowledging accounting inaccuracy: [7](#0-6) 

### Impact Explanation

`total_tx_size` is the authoritative counter used by two critical pool-admission paths:

**Pool-size enforcement (`limit_size`)** — evicts transactions while `total_tx_size > max_tx_pool_size`: [8](#0-7) 

**New-transaction admission (`updated_stat_for_add_tx`)** — rejects incoming transactions with `Reject::Full` when the counter overflows: [9](#0-8) 

Because `total_tx_size` is overstated by the aggregate size of all evicted transactions, the pool believes it is fuller than it actually is. Consequences:

- `limit_size` evicts additional legitimate pending/proposed transactions that would otherwise fit.
- Subsequent `send_transaction` / relay submissions are rejected with `Reject::Full` even though the pool has real capacity.
- The `verify_queue`'s own `total_tx_size` is a separate counter and is unaffected, creating a split-brain between the two admission gates.

### Likelihood Explanation

The eviction path is reachable by any unprivileged transaction sender via the `send_transaction` RPC or the P2P relay protocol. An attacker needs to:

1. Submit a chain of transactions where some share a cell dep, building up `cell_ref_parents` in the pool.
2. Submit a new transaction whose ancestor count exceeds `max_ancestors_count` (default 25) but whose `cell_ref_parents` count is large enough to satisfy the eviction condition.

This is a realistic sequence for a motivated attacker and requires no privileged access, no majority hashpower, and no social engineering.

### Recommendation

Compute `total_tx_size` and `total_tx_cycles` **after** `check_and_record_ancestors` completes, so that any eviction-driven decrements are already reflected in `self.total_tx_size` / `self.total_tx_cycles` before the new entry's contribution is added. Concretely, replace the pre-computation pattern with a post-eviction addition:

```rust
// After check_and_record_ancestors returns (evictions already applied):
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Or, equivalently, move the `updated_stat_for_add_tx` call to after `check_and_record_ancestors`.

### Proof of Concept

Concrete accounting trace:

| Step | Event | `self.total_tx_size` |
|---|---|---|
| Initial state | Pool has 3 txs, total size 300 | 300 |
| `updated_stat_for_add_tx(50, …)` | Pre-computes local `total_tx_size = 350` | 300 (unchanged) |
| `check_and_record_ancestors` evicts 2 txs (size 80 each) | `update_stat_for_remove_tx` called twice | 300 − 80 − 80 = **140** |
| `self.total_tx_size = total_tx_size` | Stale local written back | **350** (should be 190) |

After this sequence the pool reports 350 bytes used when only 190 bytes (1 original tx + new tx) are actually present. Every subsequent `limit_size` call will evict legitimate transactions, and every `updated_stat_for_add_tx` call will reject new transactions prematurely, constituting a sustained DoS against the transaction pool reachable by any unprivileged peer.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-211)
```rust
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
```

**File:** tx-pool/src/component/pool_map.rs (L213-213)
```rust
        evicts = self.check_and_record_ancestors(&mut entry)?;
```

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L246-247)
```rust
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

**File:** tx-pool/src/component/pool_map.rs (L716-721)
```rust
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_size {} overflows by add {}",
                self.total_tx_size, tx_size
            ))
        })?;
```

**File:** tx-pool/src/component/pool_map.rs (L731-733)
```rust
    /// Update size and cycles statistics for remove tx
    /// cycles overflow is possible, currently obtaining cycles is not accurate
    fn update_stat_for_remove_tx(&mut self, tx_size: usize, cycles: Cycle) {
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
