Audit Report

## Title
Unbounded Iteration in `get_raw_tx_pool` RPC Causes Node DoS via Memory Exhaustion and Lock Contention - (File: `rpc/src/module/pool.rs`)

## Summary
The `get_raw_tx_pool` RPC endpoint unconditionally iterates over every entry in the tx pool with no pagination, limit, or early termination. On a pool near its 180 MB capacity, a single call with `verbose=true` can allocate gigabytes of heap for JSON serialization while holding the tx-pool read lock for the entire duration, blocking transaction admission and block assembly. Any caller with RPC access can trigger this repeatedly at negligible cost.

## Finding Description
`get_raw_tx_pool` in `rpc/src/module/pool.rs` (L703–718) delegates to either `get_all_entry_info()` or `get_ids()` with no size guard. Both methods in `tx-pool/src/pool.rs` (L448–487) call `score_sorted_iter_by_statuses` and `sorted_proposed_iter` and `.collect()` the entire result into unbounded `Vec` or `HashMap` structures. In `tx-pool/src/service.rs` (L1000–1013), the async read lock (`service.tx_pool.read().await`) is acquired before the call and released only after the full collection is built and sent back to the caller. The data types `TxPoolIds` and `TxPoolEntryInfo` in `util/types/src/core/tx_pool.rs` (L158–176) are plain unbounded collections with no capacity cap. The pool is bounded by total serialized byte size (`max_tx_pool_size = 180_000_000`, i.e. 180 MB) rather than entry count, so with minimum-size transactions the pool can hold hundreds of thousands of entries. Calling `get_raw_tx_pool(verbose=true)` on such a pool: (1) holds the read lock across the full scan and serialization, blocking all concurrent write operations; (2) allocates a `HashMap<Byte32, TxEntryInfo>` for every entry, with JSON serialization overhead multiplying raw pool size by an order of magnitude; (3) can be repeated concurrently by multiple callers, compounding both effects. No existing guard, rate limit, or pagination mechanism exists anywhere in this call chain.

## Impact Explanation
This matches **High: Vulnerabilities which could easily crash a CKB node**. A node with its RPC port exposed (a common operational pattern for mining pools, exchanges, and public infrastructure) can be rendered unable to relay transactions, assemble blocks, or respond to other RPC calls. The OS OOM killer may terminate the node process outright. The read lock held during the scan starves concurrent writers (`get_block_template`, transaction submission), halting mining and relay functions even before OOM occurs. Multiple concurrent calls compound both effects. The impact is scoped to a single node, not the whole network, placing this firmly in the High tier rather than Critical.

## Likelihood Explanation
The RPC binds to `127.0.0.1:8114` by default, limiting direct remote exploitation on default-configured nodes. However, many production operators (mining pools, exchanges, public RPC providers) explicitly expose this port. The pool-filling precondition is economically trivial: at `min_fee_rate = 1,000` shannons/KB, filling 180 MB costs approximately 1.8 CKB. Alternatively, a busy mainnet node may reach a large pool state organically, requiring no attacker-controlled transactions at all. No special privileges, keys, or protocol knowledge beyond standard JSON-RPC are required. The attack is repeatable and stateless.

## Recommendation
Add pagination parameters (`limit` and `after` cursor) to `get_raw_tx_pool`, analogous to the indexer's `get_cells`/`get_transactions` endpoints. Enforce a hard per-call cap on the number of entries returned (e.g., matching the `request_limit` pattern already documented in `ckb.toml` for the indexer: `request_limit = 400`). The existing indexer RPC design in `rpc/src/module/indexer.rs` provides a ready template. Additionally, consider releasing the read lock between pages or streaming results rather than collecting the entire pool into memory before responding.

## Proof of Concept
1. Configure a CKB node with RPC exposed (or use localhost access).
2. Submit approximately N minimum-size transactions (each ~100–200 bytes) until the pool approaches `max_tx_pool_size` (180 MB). At `min_fee_rate = 1,000` shannons/KB this costs ~1.8 CKB total.
3. Issue concurrent JSON-RPC calls: `{"method":"get_raw_tx_pool","params":[true],"id":1,"jsonrpc":"2.0"}`.
4. Observe: node memory climbs rapidly as `TxPoolEntryInfo` HashMaps are allocated; the tx-pool read lock is held for the duration of each call; concurrent `send_transaction` and `get_block_template` calls stall or time out; the node process may be killed by the OOM killer.
5. Reproducible as a unit test by constructing a `TxPool` with a large number of mock entries and calling `get_all_entry_info()` while measuring allocation and lock-hold duration.