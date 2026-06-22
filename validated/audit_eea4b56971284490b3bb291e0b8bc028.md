### Title
Stale Pre-Eviction Snapshot Overwrites Correctly-Decremented `total_tx_size` in `PoolMap::add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

### Summary

In `PoolMap::add_entry`, the pool's `total_tx_size` and `total_tx_cycles` accounting counters are computed as a snapshot **before** in-pool evictions occur, then unconditionally written back **after** those evictions have already correctly decremented the live counters. This causes `total_tx_size` (and `total_tx_cycles`) to be permanently inflated by the aggregate size of every evicted transaction, mirroring the original report's pattern of updating an accounting variable with the wrong value.

---

### Finding Description

In `tx-pool/src/component/pool_map.rs`, `add_entry` follows this sequence: [1](#0-0) 

```rust
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // ← snapshot taken HERE
// ...
evicts = self.check_and_record_ancestors(&mut entry)?;          // ← evictions happen HERE
// ...
self.total_tx_size = total_tx_size;    // ← snapshot written back, overwriting eviction decrements
self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` captures `self.total_tx_size + entry.size` and `self.total_tx_cycles + entry.cycles` into local variables **before** any evictions: [2](#0-1) 

`check_and_record_ancestors` can call `remove_entry_and_descendants` when the incoming transaction's ancestor count (inflated by cell-dep references) exceeds `max_ancestors_count` but can be reduced by evicting low-fee cell-dep parents: [3](#0-2) 

Each call to `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx` correctly decrements `self.total_tx_size`: [4](#0-3) 

But immediately after, the stale snapshot is written back unconditionally: [5](#0-4) 

**Net effect:**

| Variable | Correct post-eviction value | Actual value written |
|---|---|---|
| `total_tx_size` | `original − Σ(evicted.size) + entry.size` | `original + entry.size` |
| `total_tx_cycles` | `original − Σ(evicted.cycles) + entry.cycles` | `original + entry.cycles` |

The inflation equals the sum of all evicted transactions' sizes/cycles and is never corrected (the self-healing `recompute_total_stat` path is only triggered on underflow, not overflow).

---

### Impact Explanation

`total_tx_size` is the sole counter used by `limit_size` to enforce the pool's memory cap: [6](#0-5) 

An inflated `total_tx_size` causes `limit_size` to believe the pool is over its `max_tx_pool_size` limit and to evict additional, otherwise-valid transactions. Repeated triggering of the eviction path compounds the inflation. The attacker can therefore:

1. Cause legitimate pending transactions to be evicted from the pool (tx-pool DoS / censorship).
2. Cause the pool to reject new valid transactions with `Reject::Full` even when actual pool occupancy is well below the configured limit.
3. Corrupt the `total_tx_cycles` counter, distorting fee-rate estimation returned by `estimate_fee_rate` RPC.

---

### Likelihood Explanation

The trigger condition — a new transaction whose ancestor count (via cell-dep references) exceeds `max_ancestors_count` but falls within limits after evicting cell-dep parents — is reachable by any unprivileged `send_transaction` RPC caller or P2P relay peer. The attacker needs only to:

1. Pre-populate the pool with a chain of transactions where later transactions are referenced as cell deps.
2. Submit a crafted transaction that references those cell deps and pushes the ancestor count just over the limit.

No privileged access, key material, or majority hash power is required. The attack is repeatable and the inflation accumulates across invocations.

---

### Recommendation

Move the `total_tx_size` / `total_tx_cycles` snapshot computation to **after** `check_and_record_ancestors` completes (so evictions are already reflected in `self.total_tx_size`), or compute the final values incrementally:

```diff
- let (total_tx_size, total_tx_cycles) =
-     self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
  // ...
  evicts = self.check_and_record_ancestors(&mut entry)?;
  self.record_entry_edges(&entry)?;
  self.insert_entry(&entry, status);
  self.record_entry_descendants(&entry);
  self.track_entry_statics(None, Some(status));
- self.total_tx_size = total_tx_size;
- self.total_tx_cycles = total_tx_cycles;
+ // Re-compute after evictions have already updated self.total_tx_size / self.total_tx_cycles
+ let (total_tx_size, total_tx_cycles) =
+     self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
+ self.total_tx_size = total_tx_size;
+ self.total_tx_cycles = total_tx_cycles;
```

---

### Proof of Concept

1. Fill the pool with transactions `T1 … Tk` where each `Ti` is referenced as a cell dep by `T_{i+1}`, forming a chain of length `max_ancestors_count`.
2. Submit a new transaction `Tnew` that references `T1` as a cell dep. Its ancestor count is `max_ancestors_count + 1`, exceeding the limit.
3. `check_and_record_ancestors` enters the eviction branch, evicts `T1` (and its descendants) via `remove_entry_and_descendants`. `self.total_tx_size` is decremented by `T1.size + descendants_sizes`.
4. `add_entry` then writes `self.total_tx_size = total_tx_size` (the pre-eviction snapshot), re-inflating the counter by `T1.size + descendants_sizes`.
5. Repeat steps 1–4. Each iteration inflates `total_tx_size` further.
6. Eventually `total_tx_size > max_tx_pool_size` even though the actual pool is nearly empty. `limit_size` begins evicting all remaining transactions, and new submissions are rejected with `Reject::Full`.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-220)
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
        Ok((true, evicts))
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

**File:** tx-pool/src/pool.rs (L298-327)
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
        self.pool_map.entries.shrink_to_fit();
```
