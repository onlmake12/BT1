Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Inflated by Stale Snapshot Overwrite After Evictions in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::add_entry` snapshots `total_tx_size` and `total_tx_cycles` into local variables before calling `check_and_record_ancestors`, which may evict existing pool entries and correctly subtract their sizes from `self.total_tx_size` in-place via `update_stat_for_remove_tx`. After `check_and_record_ancestors` returns, `add_entry` unconditionally overwrites `self.total_tx_size` and `self.total_tx_cycles` with the stale pre-eviction snapshot, permanently inflating both counters by the aggregate size and cycles of every evicted transaction. Because `limit_size` uses `total_tx_size` as the sole gate for pool-full evictions, an attacker can repeatedly trigger this path to make the pool appear full when it is nearly empty, causing cascading spurious evictions and `Reject::Full` errors for legitimate transactions.

## Finding Description
In `add_entry` (lines 200–221 of `tx-pool/src/component/pool_map.rs`):

```rust
// Step 1: snapshot — immutable &self call, returns self.total_tx_size + entry.size
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Step 2: &mut self call — may evict entries, calling remove_entry → update_stat_for_remove_tx
//         which directly decrements self.total_tx_size in-place (line 247)
evicts = self.check_and_record_ancestors(&mut entry)?;

// Step 3: OVERWRITES the correctly-updated self.total_tx_size with the stale snapshot
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

The code at lines 210–211 captures a pre-eviction snapshot. [1](#0-0) 

Line 213 calls `check_and_record_ancestors`, which may call `remove_entry_and_descendants` → `remove_entry`, which at line 247 calls `update_stat_for_remove_tx` and directly decrements `self.total_tx_size` in-place. [2](#0-1) [3](#0-2) 

Lines 218–219 then unconditionally overwrite `self.total_tx_size` and `self.total_tx_cycles` with the stale pre-eviction values, discarding all in-place subtractions performed during eviction. [4](#0-3) 

The net inflation per cycle equals the aggregate size of all evicted transactions. No existing guard reconciles the post-eviction state: `updated_stat_for_add_tx` only checks for integer overflow against the pre-eviction total, and there is no reconciliation step after `check_and_record_ancestors` returns.

## Impact Explanation
`limit_size` uses `self.pool_map.total_tx_size > self.config.max_tx_pool_size` as the sole eviction gate. [5](#0-4) 

Once the counter is sufficiently inflated through repeated attack iterations, `limit_size` fires on every subsequent `add_entry` call, evicting legitimate pending transactions and returning `Reject::Full` to new `send_transaction` callers even though the pool has real capacity. This constitutes a remotely triggerable denial-of-service against the transaction pool, matching the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs*.

## Likelihood Explanation
The eviction path in `check_and_record_ancestors` is reachable by any unprivileged `send_transaction` RPC caller. The attacker submits ≥ 26 transactions each referencing the same live cell as a `cell_dep`, then submits a transaction spending that cell as an input, triggering the eviction branch. Each iteration inflates `total_tx_size` by the aggregate size of the evicted transactions. No special privilege, leaked keys, or victim mistakes are required. The attack is repeatable with low transaction fees.

## Recommendation
Move the stat assignment to **after** `check_and_record_ancestors` returns, so the final write reflects the post-eviction state of `self.total_tx_size`:

```rust
// Validate capacity BEFORE evictions (overflow check only)
self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Compute final totals AFTER evictions have already updated self.total_tx_size
self.total_tx_size = self.total_tx_size
    .checked_add(entry.size)
    .expect("size overflow after evictions");
self.total_tx_cycles = self.total_tx_cycles
    .checked_add(entry.cycles)
    .expect("cycles overflow after evictions");
```

## Proof of Concept
**Setup**: Pool with `max_ancestors_count = 25`, `max_tx_pool_size = 180 MB`. Each transaction is ~600 bytes.

1. Submit 26 transactions `T1…T26`, each using cell `C` as a `cell_dep`. All accepted. `self.total_tx_size = 15 600`.
2. Submit `T_spend` spending cell `C` as an input.
   - `updated_stat_for_add_tx` snapshots `total_tx_size_local = 15 600 + size(T_spend)`.
   - `check_and_record_ancestors` finds 26 `cell_ref_parents`, evicts them; `update_stat_for_remove_tx` is called 26 times, reducing `self.total_tx_size` to `0`.
   - Lines 218–219 write `self.total_tx_size = 15 600 + size(T_spend)`.
   - **Actual pool contents**: only `T_spend`. **Reported size**: ~15 600 bytes inflated.
3. Repeat steps 1–2 `N` times. After `N` iterations, `total_tx_size ≈ N × 15 600` bytes while the pool is nearly empty.
4. Once `total_tx_size > 180 MB` (~12 800 iterations), `limit_size` begins evicting legitimate transactions and all new `send_transaction` calls return `Reject::Full`.

A unit test can directly call `pool_map.add_entry` in a loop with the described setup and assert that `pool_map.total_tx_size` equals the true sum of entry sizes after each iteration, which will fail on the current code.

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

**File:** tx-pool/src/component/pool_map.rs (L247-247)
```rust
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
```

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```
