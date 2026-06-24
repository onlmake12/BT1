Audit Report

## Title
Unbounded Full-Pool Scan in `remove_expired` on Every Block Acceptance — (`tx-pool/src/pool.rs`)

## Summary
`remove_expired` iterates every entry in the tx-pool via `pool_map.iter()` on every accepted block, holding the tx-pool write-lock for the full duration. An attacker who fills the pool to its 180 MB default limit forces O(N_total) CPU work and lock contention on every block (~every 10 seconds), degrading tx submission and RPC responsiveness proportional to pool occupancy.

## Finding Description
`remove_expired` at `tx-pool/src/pool.rs:271-288` performs an unconditional full scan:

```rust
pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
    let now_ms = ckb_systemtime::unix_time_as_millis();
    let removed: Vec<_> = self
        .pool_map
        .iter()          // full scan of ALL entries
        .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
        .map(|entry| entry.inner.clone())
        .collect();
    ...
}
```

`pool_map.iter()` resolves to `self.entries.iter().map(|(_, entry)| entry)` — a flat iteration over all pool entries with no status filter or early exit. The `PoolEntry` struct (`tx-pool/src/component/pool_map.rs:46-58`) defines indices for `id`, `score`, `status`, and `evict_key`, but **no timestamp index**, making an O(1) or O(expired) scan structurally impossible without code changes.

`remove_expired` is called unconditionally at `tx-pool/src/process.rs:1110`, outside the `if mine_mode` guard at line 1061. The write-lock is acquired at `process.rs:836` and held for the entire `_update_tx_pool_for_reorg` call, including `remove_expired` and the subsequent `limit_size` call at line 1113. Concurrent `send_transaction` and `get_block_template` calls stall for the lock duration.

The Gap/Pending loops at `process.rs:1065-1080` are correctly gated by `if mine_mode` and use the `hashed_non_unique` status index, making them O(N_status) only — the unbounded scan is solely `remove_expired`.

Exploit path:
1. Fill the pool to 180 MB via repeated `send_transaction` RPC/P2P calls paying minimum fee (~1.8 CKB at `min_fee_rate = 1000 shannons/KB`).
2. `limit_size` eviction runs only *after* `remove_expired`, so the full O(N) cost is paid on every block while the pool remains full.
3. Every ~10 seconds a new block triggers `_update_tx_pool_for_reorg` → `remove_expired` → full pool scan under the write-lock.

## Impact Explanation
**Low (501–2000 points): Any other important performance improvements for CKB.** The write-lock is held while iterating potentially hundreds of thousands of entries. Concurrent `send_transaction` and `get_block_template` calls stall for the lock duration. The effect is measurable, repeating service degradation on any node with a full pool. It does not cause consensus failure, node crash, or fund loss.

## Likelihood Explanation
Medium-low. The attacker requires no privileged access — only the ability to submit transactions via standard P2P or RPC. The cost to fill the pool is ~1.8 CKB per 12-hour expiry window. The trigger fires automatically on every block relay. The effect is limited to individual nodes and does not propagate as network-wide congestion.

## Recommendation
1. **Add a timestamp-sorted index** to `PoolEntry` (an `ordered_non_unique` index on `timestamp` alongside the existing `score` and `evict_key` indices) so `remove_expired` visits only entries near the expiry boundary.
2. **Alternatively, maintain a separate min-heap or BTreeMap keyed by `timestamp + expiry`** so `remove_expired` can break early once entries are no longer expired.
3. **Enforce a tighter pool entry count limit** (not just byte size) to bound worst-case iteration depth independently of transaction size.

## Proof of Concept
```
1. Configure a CKB node with default settings (max_tx_pool_size = 180 MB,
   min_fee_rate = 1000 shannons/KB, expiry_hours = 12).
2. Submit enough valid transactions to fill the pool to capacity via
   repeated send_transaction RPC calls. Each tx pays minimum fee.
   limit_size will evict lowest-fee entries to maintain the 180 MB cap.
3. Relay or mine a new block to the target node.
4. Instrument remove_expired (pool.rs:271) with timing:
   - Measure wall-clock time from entry to exit of remove_expired.
   - Confirm it scales linearly with pool entry count.
5. Concurrently issue send_transaction or get_block_template RPC calls
   and measure response latency during step 4.
6. Confirm latency spikes correlate with remove_expired execution under
   the write-lock, repeating every ~10 seconds.
```