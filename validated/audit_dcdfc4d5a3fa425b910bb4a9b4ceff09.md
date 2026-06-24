The code exactly matches all claims in the report. Let me verify the complete picture:

Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Stale-Snapshot Overwrite in `add_entry` Enables Phantom Pool Inflation — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

`PoolMap::add_entry` computes a prospective new total via `updated_stat_for_add_tx` before calling `check_and_record_ancestors`. When `check_and_record_ancestors` evicts entries via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, those decrements are applied in-place to `self.total_tx_size`/`self.total_tx_cycles`. The stale pre-eviction snapshot is then unconditionally written back at lines 218–219, permanently overwriting the decrements and inflating the tracked pool size by the size of each evicted entry. A repeated attacker-controlled trigger of this path causes `limit_size` to evict legitimate transactions even when the actual pool byte count is within limits.

## Finding Description

In `add_entry` (lines 200–221 of `tx-pool/src/component/pool_map.rs`):

```
210: let (total_tx_size, total_tx_cycles) =
211:     self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
213: evicts = self.check_and_record_ancestors(&mut entry)?;
...
218: self.total_tx_size = total_tx_size;
219: self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` (lines 711–729) is a pure read: it returns `self.total_tx_size + entry.size` as a local value without mutating state.

`check_and_record_ancestors` (lines 590–639) contains the eviction branch at lines 603–625: when `ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count` but `ancestors_count > self.max_ancestors_count`, it calls `remove_entry_and_descendants` in a loop. `remove_entry_and_descendants` calls `remove_entry` (line 263), which calls `update_stat_for_remove_tx` (line 247), which decrements `self.total_tx_size` in-place (lines 738–740).

After `check_and_record_ancestors` returns, `self.total_tx_size` correctly reflects the evictions. But lines 218–219 then overwrite it with the stale snapshot that was computed before any evictions occurred. The net result: `self.total_tx_size` is inflated by exactly `sum(size of evicted entries)` per `add_entry` call that triggers the eviction path.

`recompute_total_stat` is only invoked inside `update_stat_for_remove_tx` on underflow (lines 742–756); it is never called after `add_entry` returns non-empty evicts, so there is no self-correcting mechanism.

`limit_size` (pool.rs lines 292–298) loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, so phantom-inflated `total_tx_size` drives real evictions of legitimate pending/proposed transactions.

## Impact Explanation

Each trigger of the ancestor-eviction path permanently inflates `total_tx_size` by `size(evicted_entry)`. After enough iterations the phantom total exceeds `max_tx_pool_size`, causing `limit_size` to evict all real transactions from the pool even though the actual byte count is within limits. This constitutes a tx-pool denial-of-service: honest users' transactions are expelled and cannot be re-admitted while the attacker keeps re-triggering the inflation path. This matches the allowed **High** impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation

The attack requires constructing a chain of ≥ `max_ancestors_count − 1` (default: 24) transactions, one `cell_ref_parent` (a tx using a chain output as cell dep), and a new tx consuming that output as an input. This is achievable by any unprivileged `send_transaction` RPC caller with enough CKB to fund ~26 transactions per cycle. The cost is low and the attack is repeatable; each cycle adds `size(tx_dep)` phantom bytes. No privileged access, leaked keys, or victim mistakes are required.

## Recommendation

Move the `updated_stat_for_add_tx` call to after `check_and_record_ancestors` completes, so it reads the already-decremented `self.total_tx_size`/`self.total_tx_cycles` as its base:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
// self.total_tx_size now reflects any evictions
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, call `recompute_total_stat()` whenever `add_entry` returns non-empty evicts, or add a debug-build assertion that `total_tx_size == entries.iter().sum(size)` after each `add_entry`.

## Proof of Concept

1. Set `max_ancestors_count = 25`, `max_tx_pool_size = 20 MB`.
2. Submit `tx_0` creating `cell_0`.
3. Submit `tx_1 … tx_24` as a chain (each spends the previous output).
4. Submit `tx_dep` using `tx_24`'s output as a **cell dep** — it becomes a `cell_ref_parent`.
5. Submit `tx_25` spending `tx_24`'s output as an **input**. `ancestors_count = 26 > 25`; `check_and_record_ancestors` evicts `tx_dep` via `remove_entry_and_descendants` → `self.total_tx_size -= size(tx_dep)`. `add_entry` then writes back the stale snapshot: `self.total_tx_size = old_total + size(tx_25)`. **Net inflation: `+size(tx_dep)`**.
6. Repeat steps 4–5 with fresh outputs. Each iteration adds `size(tx_dep)` phantom bytes.
7. Once phantom `total_tx_size > max_tx_pool_size`, `limit_size` evicts all real pool entries even though actual pool bytes are within limits. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** tx-pool/src/pool.rs (L297-299)
```rust
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
```
