Audit Report

## Title
`total_tx_size` / `total_tx_cycles` Overcounted When Evictions Occur During `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, a pre-eviction snapshot of `total_tx_size` and `total_tx_cycles` is captured before `check_and_record_ancestors` runs, but is written back to `self` after that function has already correctly decremented those fields via evictions. The stale snapshot permanently overwrites the correct post-eviction values, inflating both counters by the total size/cycles of every evicted transaction. An unprivileged attacker can repeat this to drive `total_tx_size` to the pool limit, causing all subsequent `send_transaction` calls to return `Reject::Full`.

## Finding Description

`add_entry` executes the following sequence:

**Step 1** — `updated_stat_for_add_tx` takes `&self` and returns `(self.total_tx_size + tx_size, self.total_tx_cycles + cycles)` without modifying any field. The result is stored in local variables `total_tx_size` / `total_tx_cycles`. [1](#0-0) 

**Step 2** — `check_and_record_ancestors` may enter the eviction branch when `ancestors_count > max_ancestors_count` and the excess ancestors are all `cell_ref_parents`. It calls `remove_entry_and_descendants` for each evicted candidate. [2](#0-1) 

`remove_entry_and_descendants` calls `remove_entry` for each removed transaction, which calls `update_stat_for_remove_tx`, directly decrementing `self.total_tx_size` and `self.total_tx_cycles`. [3](#0-2) [4](#0-3) 

**Step 3** — After all evictions have correctly updated `self.total_tx_size`, the stale pre-eviction snapshot is unconditionally written back, overwriting the correct value: [5](#0-4) 

**Concrete arithmetic:**

| Step | `self.total_tx_size` | local `total_tx_size` |
|---|---|---|
| Initial | `X` | — |
| After `updated_stat_for_add_tx` | `X` (unchanged) | `X + new_size` |
| After evicting `E` bytes | `X − E` (correct) | `X + new_size` (stale) |
| After line 218 | `X + new_size` **(wrong)** | — |

Correct value should be `X − E + new_size`. The counter is inflated by `E` on every eviction-during-add.

The `recompute_total_stat` fallback only fires on underflow during removal, not on this overcount path. [6](#0-5) 

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

1. `limit_size` compares `pool_map.total_tx_size` against `config.max_tx_pool_size` in a loop. An inflated counter causes the pool to believe it is over-limit, triggering unnecessary eviction of legitimate pending transactions. [7](#0-6) 

2. Subsequent calls to `updated_stat_for_add_tx` read the inflated `self.total_tx_size` and return `Reject::Full` for transactions that would fit within the real pool budget, effectively blocking all new transaction submissions. [8](#0-7) 

3. `TxPoolInfo.total_tx_size` exposed via the `tx_pool_info` RPC is read directly from `pool_map.total_tx_size`, so wallets and relayers see false pool occupancy. [9](#0-8) 

4. Each successful eviction-during-add inflates the counter further. The drift is cumulative and permanent until node restart.

## Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged `send_transaction` RPC caller. The attacker needs only to:

1. Submit a root transaction `T0` with output `O0`.
2. Submit many transactions `C1…Cn` that each cell-dep on `O0` (not spending it), so each has only 1 ancestor. All are accepted.
3. Submit a transaction that spends `O0` as an input. This transaction now has `ancestors_count > max_ancestors_count` with the excess being `cell_ref_parents`, triggering the eviction loop inside `check_and_record_ancestors`. [10](#0-9) 

No special privilege, key material, or majority hash power is required. The attack is repeatable: each iteration inflates `total_tx_size` by the cumulative size of evicted transactions, and the loop can be driven until `total_tx_size` exceeds `max_tx_pool_size`.

## Recommendation

Compute the new totals **after** `check_and_record_ancestors` completes, so evictions are already reflected in `self.total_tx_size` before the new entry's contribution is added:

```rust
// Pre-check for overflow only; do NOT capture the new totals yet.
self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Compute totals AFTER evictions have already updated self.total_tx_size/cycles.
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

This preserves the overflow pre-check while eliminating the stale snapshot overwrite.

## Proof of Concept

1. Configure a node with `max_ancestors_count = 25` (default).
2. Submit root transaction `T0` with output `O0`.
3. Submit 26 transactions `C1…C26`, each spending an independent input but cell-depping on `O0`. All are accepted (each has 1 ancestor: itself). Record `pool_info.total_tx_size = S`.
4. Submit transaction `T_consume` spending `O0` as an input. This triggers `check_and_record_ancestors`: `ancestors_count = 27 > 25`, all excess are `cell_ref_parents`. Two transactions (e.g., `C1`, `C2`) are evicted via `remove_entry_and_descendants`, correctly decrementing `self.total_tx_size`. Then lines 218–219 overwrite with the stale snapshot. [11](#0-10) 
5. Observe via RPC that `pool_info.total_tx_size` is `S + size(T_consume)` instead of `S - size(C1) - size(C2) + size(T_consume)`.
6. Repeat steps 2–5 to accumulate inflation until `total_tx_size` exceeds `max_tx_pool_size`, at which point all subsequent `send_transaction` calls return `Reject::Full` even though the pool has ample real space.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-211)
```rust
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
```

**File:** tx-pool/src/component/pool_map.rs (L213-219)
```rust
        evicts = self.check_and_record_ancestors(&mut entry)?;
        self.record_entry_edges(&entry)?;
        self.insert_entry(&entry, status);
        self.record_entry_descendants(&entry);
        self.track_entry_statics(None, Some(status));
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
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

**File:** tx-pool/src/component/pool_map.rs (L716-721)
```rust
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_size {} overflows by add {}",
                self.total_tx_size, tx_size
            ))
        })?;
```

**File:** tx-pool/src/component/pool_map.rs (L738-741)
```rust
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
            }
```

**File:** tx-pool/src/component/pool_map.rs (L742-749)
```rust
            _ => {
                if let Some((total_tx_size, total_tx_cycles)) = self.recompute_total_stat() {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, recomputed size {} cycles {}",
                        tx_size, cycles, total_tx_size, total_tx_cycles
                    );
                    self.total_tx_size = total_tx_size;
                    self.total_tx_cycles = total_tx_cycles;
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

**File:** tx-pool/src/service.rs (L1089-1090)
```rust
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
```
