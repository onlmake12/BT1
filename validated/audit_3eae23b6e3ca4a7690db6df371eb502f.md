The code confirms the claim. Here is the validation:

---

Audit Report

## Title
Stale Pre-Eviction Totals Overwrite Post-Eviction `total_tx_size`/`total_tx_cycles` in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` is called before `check_and_record_ancestors`, capturing a pre-eviction snapshot of `self.total_tx_size + entry.size` into local variables. When `check_and_record_ancestors` evicts transactions via `remove_entry_and_descendants → remove_entry → update_stat_for_remove_tx`, `self.total_tx_size` is correctly decremented. However, the stale locals are then unconditionally written back at L218–219, erasing those decrements and permanently inflating the counter by the size of all evicted transactions.

## Finding Description
The exact sequence in `add_entry` (L210–219):

```
L210-211: (total_tx_size, total_tx_cycles) =
              self.updated_stat_for_add_tx(entry.size, entry.cycles)?
          // pure read: returns (self.total_tx_size + entry.size, ...)
          // snapshot = S + new_size; self.total_tx_size still = S

L213:     evicts = self.check_and_record_ancestors(&mut entry)?
          // eviction path (L616-618): remove_entry_and_descendants(next_id)
          //   → remove_entry (L235-249)
          //     → update_stat_for_remove_tx (L733-757)
          //       → self.total_tx_size -= evicted_size
          // now self.total_tx_size = S - evicted_size

L218:     self.total_tx_size = total_tx_size
          // overwrites with S + new_size  ← BUG
          // correct value: S + new_size - evicted_size
```

`updated_stat_for_add_tx` (L711–729) is confirmed to be a pure read — it takes `&self` and returns a new value without mutating state. [1](#0-0) 

`update_stat_for_remove_tx` (L733–757) directly assigns to `self.total_tx_size` and `self.total_tx_cycles`. [2](#0-1) 

The eviction path in `check_and_record_ancestors` (L616–618) is reached when `ancestors_count > self.max_ancestors_count` and `cell_ref_parents` is non-empty, calling `remove_entry_and_descendants` which chains to `remove_entry → update_stat_for_remove_tx`. [3](#0-2) 

The unconditional overwrite at L218–219 then replaces the correctly decremented `self.total_tx_size` with the stale snapshot. [4](#0-3) 

## Impact Explanation
`limit_size` (pool.rs L298) loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting lowest-fee-rate transactions. [5](#0-4)  An inflated counter causes it to evict valid, fee-paying transactions that should remain. Subsequent calls to `updated_stat_for_add_tx` start from the inflated baseline, causing valid transactions to be rejected with `Reject::Full`. Repeated triggering accumulates inflation, progressively degrading mempool capacity. This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
The trigger requires a new transaction whose ancestor count (including `cell_ref_parents`) exceeds `max_ancestors_count` (default 25), with at least one `cell_ref_parent` evictable to bring the count within limit. An attacker observing the mempool via `get_raw_tx_pool` can construct this deliberately: build a 24-deep chain, submit a cell-dep consumer `R`, then submit `T_new` spending the same output. No special privilege is required — only the ability to submit transactions via `send_transaction` or P2P relay. The condition can also arise organically. The attack is repeatable to accumulate inflation.

## Recommendation
Move `updated_stat_for_add_tx` to after `check_and_record_ancestors` completes, so the snapshot reflects the post-eviction state:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size  = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, replace the local-variable pattern with in-place increments (`self.total_tx_size += entry.size`) applied only after all mutations are complete, handling overflow separately.

## Proof of Concept
1. Fill the pool with a chain `T1 → T2 → … → T24` (each spending the previous output).
2. Submit transaction `R` that takes `T1`'s output as a **cell dep** (`R` becomes a `cell_ref_parent`).
3. Submit `T_new` spending `T1`'s output. Ancestor count = 24 + `R` = 25, exceeding `max_ancestors_count`.
4. `check_and_record_ancestors` evicts `R` (size `S_R`) via `remove_entry_and_descendants`, decrementing `self.total_tx_size` by `S_R`.
5. L218 overwrites `self.total_tx_size` with the pre-eviction snapshot, inflating it by `S_R`.
6. Call `get_tip_tx_pool_info` and compare `total_tx_size` against the sum of actual entry sizes — the counter exceeds the real sum by `S_R`.
7. Observe that `limit_size` subsequently evicts a valid transaction that fits within `max_tx_pool_size`, confirming spurious eviction.
8. Repeat steps 1–5 to accumulate inflation and eventually cause `Reject::Full` for transactions that fit within the real pool capacity.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L616-621)
```rust
            while ancestors_count > self.max_ancestors_count {
                if let Some(next_id) = iter.next() {
                    let removed = self.remove_entry_and_descendants(next_id);
                    ancestors_count = ancestors_count.saturating_sub(1);
                    parents.remove(next_id);
                    evicted.extend(removed);
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

**File:** tx-pool/src/pool.rs (L297-307)
```rust
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
```
