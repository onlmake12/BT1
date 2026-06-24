Audit Report

## Title
Stale Pre-Eviction Totals Overwrite In-Place Accounting in `add_entry`, Inflating `total_tx_size`/`total_tx_cycles` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, `updated_stat_for_add_tx` snapshots `self.total_tx_size + entry.size` into local variables before `check_and_record_ancestors` executes. When that function evicts cell-ref-parent transactions via `remove_entry_and_descendants → remove_entry → update_stat_for_remove_tx`, it correctly decrements `self.total_tx_size` in-place. The final two lines of `add_entry` then unconditionally overwrite those decrements with the stale pre-eviction snapshot, permanently inflating both counters by the size and cycles of every evicted transaction. The inflated `total_tx_size` causes `limit_size` to evict legitimate pending/proposed transactions from the pool.

## Finding Description

`add_entry` (L200–221) executes in this order:

```rust
// L210-211: snapshot into LOCAL variables; self.* unchanged
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// L213: may call remove_entry_and_descendants → remove_entry →
//        update_stat_for_remove_tx, which WRITES BACK to self.total_tx_size
evicts = self.check_and_record_ancestors(&mut entry)?;

// L218-219: OVERWRITES the in-place decrements with the stale snapshot
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` (L711–729) is a pure read: it returns `self.total_tx_size + tx_size` without modifying `self`.

`update_stat_for_remove_tx` (L733–758) directly writes `self.total_tx_size = self.total_tx_size - tx_size`.

The eviction path in `check_and_record_ancestors` (L603–625) fires when `ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count`, calling `remove_entry_and_descendants` for each cell-ref-parent candidate. Each such removal decrements `self.total_tx_size` in-place. The unconditional assignment at L218–219 then restores the pre-eviction value, losing all those decrements.

**Accounting table:**

| Step | `self.total_tx_size` | local `total_tx_size` |
|---|---|---|
| Initial | X | — |
| After `updated_stat_for_add_tx(entry_size)` | X (unchanged) | X + entry_size |
| After eviction of tx with `evicted_size` | X − evicted_size | X + entry_size (stale) |
| After `self.total_tx_size = total_tx_size` | **X + entry_size (wrong)** | — |

Correct value: `X − evicted_size + entry_size`. Inflation per attack iteration: `evicted_size`.

`limit_size` (L298) uses `self.pool_map.total_tx_size > self.config.max_tx_pool_size` as its sole eviction guard. With an inflated counter, it evicts legitimate pending/proposed transactions and fires `Reject` callbacks for each, permanently removing them from the pool.

## Impact Explanation

An unprivileged attacker can repeatedly submit transactions that trigger the cell-ref-parent eviction path, accumulating inflation in `total_tx_size` with each submission. Once the inflated value crosses `max_tx_pool_size`, `limit_size` begins evicting honest transactions. Evicted transactions must be resubmitted by their originators, generating repeated rebroadcast traffic across the network. This constitutes a low-cost, repeatable disruption of normal mempool operation reachable via the public `send_raw_transaction` RPC — matching **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation

The trigger requires no privileged access, key material, or majority hashpower. Any caller of `send_raw_transaction` can:
1. Pre-populate the pool with transactions sharing a common cell dep output (creating cell-ref-parent relationships).
2. Submit a new transaction whose `ancestors_count` exceeds `max_ancestors_count` only because of those cell-ref-parents, satisfying the condition at L603.
3. Each such submission inflates `total_tx_size` by the evicted transactions' sizes.
4. Repeat until `limit_size` begins evicting honest transactions.

The condition at L603 (`ancestors_count - cell_ref_parents.len() <= max_ancestors_count`) is a normal, reachable code path, not an edge case.

## Recommendation

Move `updated_stat_for_add_tx` to after `check_and_record_ancestors` completes, so it reads the already-decremented `self.total_tx_size`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Compute AFTER evictions have modified self.total_tx_size
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, replace the local-variable pattern with direct in-place increments (`self.total_tx_size += entry.size`) after all mutations complete, mirroring the pattern already used in `update_stat_for_remove_tx`.

## Proof of Concept

```
Setup: max_tx_pool_size = 200_000, max_ancestors_count = 2
       Pool starts with total_tx_size = 190_000

1. Attacker submits tx_A (size=5_000) and tx_B (size=5_000), both referencing
   cell dep output O. Both enter pool. total_tx_size = 200_000.

2. Attacker submits tx_C (size=1_000) spending output O.
   - ancestors_count = 3 (tx_A, tx_B, tx_C)
   - cell_ref_parents = {tx_A, tx_B} (len=2)
   - 3 - 2 = 1 <= max_ancestors_count=2 → eviction path fires (L603)

3. check_and_record_ancestors evicts tx_A (5_000 bytes):
   update_stat_for_remove_tx(5_000) → self.total_tx_size = 195_000

4. add_entry L218: self.total_tx_size = total_tx_size = 201_000
   (pre-eviction 200_000 + entry 1_000, ignoring the −5_000 decrement)

5. Correct: 200_000 − 5_000 + 1_000 = 196_000
   Actual:   201_000  (inflated by 5_000)

6. limit_size sees 201_000 > 200_000 → evicts an honest pending transaction.

Repeat step 2 with fresh cell-ref-parent setups to accumulate further inflation.
```