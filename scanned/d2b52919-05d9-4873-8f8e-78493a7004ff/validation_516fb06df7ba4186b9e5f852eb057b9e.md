Audit Report

## Title
Unbounded Linear Pool Scan in `estimate_fee_rate` Fallback Enables RPC-Triggered Lock Contention and Throughput Degradation — (`tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::estimate_fee_rate` performs an O(n) scan over every entry in the tx-pool with no iteration cap. An unprivileged attacker who fills the pool with minimum-size, minimum-fee transactions and then calls `estimate_fee_rate` in a tight loop can hold the pool's `RwLock` read guard for extended periods, starving concurrent write operations (new transaction admission, block assembly) and degrading node throughput. No authentication or rate-limiting guards this RPC endpoint.

## Finding Description
The fallback path in `PoolMap::estimate_fee_rate` iterates the full score-sorted entry set unconditionally:

```rust
// tx-pool/src/component/pool_map.rs L342-358
let iter = self.entries.iter_by_score().rev();   // full pool, no cap
for entry in iter {
    current_block_bytes += entry.inner.size;
    current_block_cycles += entry.inner.cycles;
    if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
        target_blocks -= 1;
        if target_blocks == 0 { return entry.inner.fee_rate(); }
        ...
    }
}
min_fee_rate   // reached only after visiting every entry
```

The only early exit requires accumulating `target_blocks × max_block_bytes` worth of data. With minimum-size transactions (~100 bytes each) and `max_block_bytes` ~500 KB, the loop must visit ~5,000 entries per target block before the threshold is crossed once. If the pool is packed with such transactions, the function falls through to `return min_fee_rate` after scanning every entry.

The service message handler acquires the pool's `RwLock` read guard before invoking this function (consistent with all other handlers, e.g. `GetAllEntryInfo` at service.rs L1001). Tokio's `RwLock` blocks writers while any reader holds the guard, so concurrent write operations — `submit_transaction`, block commit, pool eviction — queue behind every in-flight scan.

The RPC entry point defaults `enable_fallback = true` with no rate-limiting:

```rust
// rpc/src/module/experiment.rs L306-307
let enable_fallback = enable_fallback.unwrap_or(true);
```

No authentication, per-client quota, or per-call entry cap exists anywhere in the RPC layer (confirmed: zero matches for `rate_limit`, `ratelimit`, `throttle` in `rpc/src/`).

The parallel unbounded collect in `get_all_entry_info` (pool.rs L464-487) compounds the issue: it also holds the read lock while allocating proportional heap memory for every pending, gap, and proposed entry.

## Impact Explanation
This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker holding the pool read lock across repeated O(n) scans serialises all write-path operations. Transaction propagation latency increases proportionally to the number of concurrent attacker RPC calls. Block assembly (`get_block_template`) also acquires the pool lock and is similarly delayed. A sustained attack degrades the node's ability to relay transactions and produce blocks on schedule, contributing to network-level congestion. The attacker's cost is bounded by `max_tx_pool_size` (a one-time pool-fill) plus essentially free RPC calls; the victim's cost scales with pool occupancy per call.

## Likelihood Explanation
- `estimate_fee_rate` is publicly documented, enabled by default, and requires no credentials.
- `enable_fallback` defaults to `true`; no special parameter is needed.
- Filling the pool with minimum-fee transactions is cheap: the attacker pays only the minimum fee rate on valid transactions, which can be replaced or evicted without being mined.
- The attack is repeatable and stateless: the attacker re-calls the RPC in a loop with no per-call cost beyond network round-trip.
- No existing guard (authentication, rate-limit, entry cap) prevents this.

## Recommendation
1. **Cap the scan**: introduce a `max_entries` bound in `estimate_fee_rate`, e.g. `target_blocks * MAX_BLOCK_PROPOSALS_LIMIT`, and break early:
   ```rust
   let max_entries = target_blocks * MAX_BLOCK_PROPOSALS_LIMIT;
   for entry in iter.take(max_entries) { ... }
   ```
2. **Paginate `get_all_entry_info` / `get_raw_tx_pool`**: add optional `limit`/`cursor` parameters to prevent unbounded heap allocation in a single call.
3. **Rate-limit RPC endpoints** at the server layer (e.g., per-IP token bucket) for `estimate_fee_rate` and `get_raw_tx_pool`.

## Proof of Concept
1. Fill the pool with `N` transactions of minimum size (~100 bytes) and minimum fee rate until `max_tx_pool_size` is reached. Each transaction is valid and accepted.
2. From a separate client, call `estimate_fee_rate` (with default parameters) in a tight loop.
3. Observe: each call iterates all `N` entries while holding the read lock. Concurrent `submit_transaction` RPC calls experience increasing latency or timeout as the write lock is starved.
4. Measure block template generation latency (`get_block_template`) before and during the attack to confirm degradation.
5. A unit test can be constructed by populating a `PoolMap` with `MAX_TX_POOL_SIZE / MIN_TX_SIZE` entries and timing `estimate_fee_rate` to confirm O(n) growth.