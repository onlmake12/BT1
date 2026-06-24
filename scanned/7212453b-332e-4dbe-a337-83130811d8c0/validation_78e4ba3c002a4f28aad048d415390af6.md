Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Overwritten With Stale Pre-Eviction Value in `add_entry` - (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, projected pool totals are computed before `check_and_record_ancestors` runs, but written back after it returns. When `check_and_record_ancestors` evicts cell-ref parent transactions to satisfy the ancestor-count limit, those evictions correctly decrement `self.total_tx_size` via `update_stat_for_remove_tx`. The subsequent unconditional assignment `self.total_tx_size = total_tx_size` then overwrites the eviction-adjusted value with the stale pre-eviction figure, permanently inflating the tracked pool size by the total byte-size of all evicted entries. An unprivileged attacker can repeat this to drive `total_tx_size` arbitrarily high, causing the pool to reject all new submissions with `Reject::Full` and triggering spurious evictions of legitimate transactions.

## Finding Description

In `add_entry` (L210-219), the sequence is:

```
// Step 1: snapshot projected totals from current self.total_tx_size
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // L210-211

// Step 2: may evict cell-ref parents via remove_entry_and_descendants,
//         each of which calls update_stat_for_remove_tx and MODIFIES self.total_tx_size
evicts = self.check_and_record_ancestors(&mut entry)?;          // L213

// Step 3: OVERWRITES the eviction-adjusted self.total_tx_size with the stale snapshot
self.total_tx_size = total_tx_size;                             // L218
self.total_tx_cycles = total_tx_cycles;                         // L219
```

The eviction path inside `check_and_record_ancestors` (L603-625) is reached when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. In that case, lowest-fee cell-ref parents are removed via `remove_entry_and_descendants` (L618), which internally calls `update_stat_for_remove_tx` and decrements `self.total_tx_size` in-place (L738-740). The final write-back at L218 discards those decrements entirely.

`updated_stat_for_add_tx` (L716) computes `self.total_tx_size + new_tx_size` from the current field value at call time. Because the field is read before evictions occur, the local snapshot does not account for them. After `add_entry` returns, `self.total_tx_size` equals `old_total + new_tx_size` instead of the correct `old_total + new_tx_size - evicted_sizes`.

The `update_stat_for_remove_tx` fallback (L742-756) only triggers on underflow; it never corrects an inflation, so the error accumulates indefinitely.

## Impact Explanation

`limit_size` in `pool.rs` (L298) uses `self.pool_map.total_tx_size > self.config.max_tx_pool_size` as its sole eviction trigger. An inflated counter causes it to evict legitimate pending transactions that should remain in the pool. Additionally, `updated_stat_for_add_tx` (L716-721) rejects incoming transactions with `Reject::Full` based on the same inflated counter. Repeated triggering accumulates inflation without bound, eventually making the pool appear permanently full and blocking all new transaction submissions from any user. This constitutes a low-cost, remotely triggerable denial-of-service against the transaction pool, matching the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation

No privileged access, key material, or majority hash power is required. An unprivileged submitter can craft the required structure: a transaction chain where at least one transaction is referenced as a cell dep by another pool entry (creating a cell-ref parent), and then submit a new transaction whose ancestor count exceeds `max_ancestors_count` (default 25) but can be reduced to ≤ 25 by evicting those cell-ref parents. Each such submission inflates `total_tx_size` by the byte-size of the evicted entries. The attack is repeatable and cumulative.

## Recommendation

Move the `updated_stat_for_add_tx` call to after `check_and_record_ancestors` returns, so that any evictions are already reflected in `self.total_tx_size` before the projected new total is computed:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute AFTER evictions have adjusted self.total_tx_size
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, capture the total evicted size from the returned `HashSet<TxEntry>` and subtract it from the pre-computed `total_tx_size` before writing back.

## Proof of Concept

Assume `max_ancestors_count = 25`, `max_tx_pool_size = 10000`, `self.total_tx_size = 1000`.

1. Submit tx₁ (size=200) as a cell-dep parent of tx₂.
2. Submit tx₂ through tx₂₆ forming a 26-deep ancestor chain where tx₁ is a cell-ref parent of tx₂₆.
3. Submit tx₂₇ (size=100) depending on tx₂₆ → ancestor count = 27 > 25.
4. `updated_stat_for_add_tx(100, ...)` snapshots local `total_tx_size = 1100`.
5. `check_and_record_ancestors` evicts tx₁ (size=200) → `self.total_tx_size` becomes 800 (1000 - 200).
6. `self.total_tx_size = 1100` overwrites the correct value of 900 (800 + 100).
7. Pool now reports 1100 instead of 900; 200-byte inflation persists.
8. Repeat steps 1-7 with fresh transactions; each iteration adds another 200-byte inflation.
9. After ~45 iterations, `total_tx_size` exceeds `max_tx_pool_size = 10000` despite the pool holding far fewer bytes, causing `Reject::Full` for all subsequent submissions.

A unit test can verify this by calling `add_entry` with a crafted entry that triggers the cell-ref-parent eviction path and asserting that `pool_map.total_tx_size` equals the sum of sizes of all entries actually remaining in the pool after the call.