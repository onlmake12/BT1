### Title
`add_entry` applies pre-computed `total_tx_size`/`total_tx_cycles` after ancestor-eviction already decremented them, causing pool accounting overcount - (File: tx-pool/src/component/pool_map.rs)

---

### Summary

In `PoolMap::add_entry`, the new pool-size totals are computed **before** `check_and_record_ancestors` runs, but written back **after** it. `check_and_record_ancestors` can evict existing pool entries (calling `remove_entry` → `update_stat_for_remove_tx`), which decrements `total_tx_size` and `total_tx_cycles` in place. The pre-computed values then overwrite those decremented values, permanently overcounting the pool's size and cycle usage by the sum of all evicted transactions' sizes/cycles. The overcounted totals feed directly into `limit_size`, which then evicts legitimate third-party transactions to compensate for phantom capacity that does not exist.

---

### Finding Description

`PoolMap::add_entry` (lines 200–221) follows this sequence:

```
1. (total_tx_size, total_tx_cycles) = updated_stat_for_add_tx(entry.size, entry.cycles)
   // snapshot: new_total = self.total_tx_size + entry.