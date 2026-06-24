Audit Report

## Title
`total_tx_size` Inflated by Evicted Transactions in `PoolMap::add_entry`, Causing Tx-Pool DoS — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, `updated_stat_for_add_tx` snapshots `total_tx_size = X + new_size` before ancestor-eviction occurs. When `check_and_record_ancestors` evicts `cell_ref_parents` via `remove_entry_and_descendants`, `update_stat_for_remove_tx` decrements `self.total_tx_size` to `X - evicted_size`. Lines 218–219 then unconditionally overwrite `self.total_tx_size` with the stale pre-eviction snapshot `X + new_size`, permanently discarding the eviction decrements. Repeated triggering inflates `total_tx_size` until it permanently exceeds `max_tx_pool_size`, causing `limit_size` to evict every newly submitted transaction and blocking all tx-pool submissions.

## Finding Description

**Root cause — stale snapshot overwrite:**

`add_entry` (lines 200–221) computes a snapshot of the post-add totals before any eviction:

```rust
// line 210-211
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
// snapshot = self.total_tx_size + entry.size  (no mutation yet)
``` [1](#0-0) 

`check_and_record_ancestors` (line 213) may then evict `cell_ref_parents` entries:

```rust
// line 618 inside check_and_record_ancestors
let removed = self.remove_entry_and_descendants(next_id);
``` [2](#0-1) 

Each `remove_entry_and_descendants` call reaches `update_stat_for_remove_tx`, which directly mutates `self.total_tx_size`:

```rust
// lines 738-740
self.total_tx_size = total_tx_size;   // self.total_tx_size = X - evicted_size
self.total_tx_cycles = total_tx_cycles;
``` [3](#0-2) 

After `check_and_record_ancestors` returns, lines 218–219 overwrite the now-correct `self.total_tx_size` with the stale snapshot:

```rust
self.total_tx_size = total_tx_size;   // reverts to X + new_size
self.total_tx_cycles = total_tx_cycles;
``` [4](#0-3) 

**Correct post-state:** `X - evicted_size + new_size`
**Actual post-state:** `X + new_size`

The eviction path fires when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (line 603), a condition reachable by any submitter. [5](#0-4) 

## Impact Explanation

`limit_size` uses `total_tx_size` as its sole guard:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size { … }
``` [6](#0-5) 

Once accumulated inflation causes `total_tx_size` to permanently exceed `max_tx_pool_size` (default 180 MB) even with a nearly empty pool, every `submit_entry` call triggers `limit_size`, which immediately evicts the just-inserted transaction. This blocks all new transaction submissions and miner block assembly on the targeted node.

**Impact class: High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** Applied at scale across multiple nodes (each independently exploitable via `send_raw_transaction` RPC or P2P relay), this constitutes network-wide congestion with minimal attacker cost.

## Likelihood Explanation

The trigger requires no privileged access, no majority hashpower, and no Sybil attack. Any unprivileged user can craft the required transaction graph using the public `send_raw_transaction` RPC. The eviction branch is a documented, intentional code path (not an edge case), making it reliably reachable. Each trigger inflates `total_tx_size` by the sum of sizes of evicted transactions; with default limits (~180 MB pool, ~1 MB transactions), roughly 90–180 trigger cycles suffice to saturate the counter.

## Recommendation

Remove the pre-computed snapshot pattern entirely. Instead, apply the addition directly to `self.total_tx_size` **after** `check_and_record_ancestors` completes, so it operates on the post-eviction baseline:

```rust
// After check_and_record_ancestors, record_entry_edges, insert_entry, etc.
self.total_tx_size = self.total_tx_size
    .checked_add(entry.size)
    .expect("overflow checked above");
self.total_tx_cycles = self.total_tx_cycles
    .checked_add(entry.cycles)
    .expect("overflow checked above");
```

The overflow check currently done by `updated_stat_for_add_tx` can be retained as an early guard (returning `Reject::Full` on overflow) but must not capture a snapshot that is later written back.

## Proof of Concept

**Setup** (default config: `max_ancestors_count = 25`, `max_tx_pool_size = 180_000_000`):

1. Submit `T0` (large tx, ~1 MB).
2. Submit 5 transactions `T1…T5`, each using `T0`'s output as a **cell-dep** (not input). Each ~1 MB. These become `cell_ref_parents` of any future tx spending `T0`'s output.
3. Build a 25-deep input chain `C1 → C2 → … → C25` in the pool.
4. Submit `Tnew` that spends `C25`'s output (25 input-chain ancestors) and uses `T0`'s output as a cell-dep. `ancestors_count = 26 > 25`; `26 - 5 = 21 ≤ 25` → eviction branch fires.
5. `check_and_record_ancestors` evicts some of `T1…T5` (e.g., 2 entries, ~2 MB). `update_stat_for_remove_tx` decrements `self.total_tx_size` by ~2 MB.
6. Lines 218–219 overwrite `self.total_tx_size` with the pre-eviction snapshot + `Tnew.size`, losing the 2 MB decrement.
7. Repeat steps 3–6 ~90 times → `total_tx_size` exceeds 180 MB while the actual pool holds far fewer bytes.
8. Every subsequent `send_raw_transaction` call triggers `limit_size`, which immediately evicts the submitted tx → **tx-pool DoS**.

A unit test can verify this by asserting `pool_map.total_tx_size == pool_map.recompute_total_stat().0` after each `add_entry` call that triggers the eviction path; the assertion will fail on the current code. [7](#0-6)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-211)
```rust
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
```

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
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

**File:** tx-pool/src/component/pool_map.rs (L738-740)
```rust
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```
