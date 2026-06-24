Audit Report

## Title
Unbounded Iteration in `get_raw_tx_pool` RPC Causes Node DoS via Memory Exhaustion and Lock Contention - (File: `rpc/src/module/pool.rs`)

## Summary
The `get_raw_tx_pool` RPC endpoint unconditionally iterates over every entry in the transaction pool with no pagination, no result-size cap, and no early termination. With the default pool size of 180 MB and minimum-size transactions, a single call with `verbose=true` can allocate gigabytes of heap and hold the tx-pool async read lock for the full duration of iteration and serialization, blocking all concurrent write operations including block assembly.

## Finding Description
All cited code is confirmed to match the claim exactly.

`get_raw_tx_pool` in `rpc/src/module/pool.rs` (L703–718) delegates unconditionally to either `get_all_entry_info()` or `get_ids()` with no limit parameter. Both methods in `tx-pool/src/pool.rs` (L448–487) call `.collect()` on unbounded iterators over the full pool contents. In `tx-pool/src/service.rs` (L1000–1013), the message handler acquires `service.tx_pool.read().await` and holds it across the entire `get_all_entry_info()` / `get_ids()` call before releasing. The result types `TxPoolEntryInfo` and `TxPoolIds` in `util/types/src/core/tx_pool.rs` (L158–176) are plain `HashMap` and `Vec` with no capacity bounds.

The default pool size is confirmed at `DEFAULT_MAX_TX_POOL_SIZE = 180_000_000` bytes (180 MB) in `util/app-config/src/legacy/tx_pool.rs` (L20) and `resource/ckb.toml` (L211). With minimum-size transactions (~100–200 bytes), the pool can hold hundreds of thousands of entries. The verbose path allocates a full `TxEntryInfo` struct per entry (containing size, cycles, fee, timestamps, ancestor counts, etc.), then serializes the entire result to JSON — producing an in-memory footprint that can be an order of magnitude larger than the raw pool data.

No pagination, rate limiting, or result-size guard exists anywhere in this call chain.

## Impact Explanation
The concrete impact is **node crash via OOM**: the OS OOM killer terminates the node process when heap allocation for the response exceeds available memory. Concurrently, the tokio async read lock held during the full scan blocks all tx-pool write operations (new transaction admission, block template assembly via `get_block_template`), degrading or halting node operation for the lock duration. Multiple concurrent calls compound both effects. This maps to **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
By default the RPC binds to `127.0.0.1:8114` (localhost only, confirmed in `resource/ckb.toml` L182), which limits direct external exploitation. However: (1) many infrastructure operators explicitly expose the RPC port publicly; (2) any local process on the same host can trigger this without credentials — no authentication mechanism exists on the Pool RPC module; (3) the pool can reach a large state organically on a busy mainnet node without attacker intervention; (4) the call is repeatable with zero per-call cost once the pool is full.

## Recommendation
Add pagination to `get_raw_tx_pool` with `limit` and `after` cursor parameters, mirroring the existing pattern in the indexer RPC. Enforce a hard cap on entries returned per call. The `indexer_v2` section in `ckb.toml` already documents a `request_limit` field (L291) as precedent. Alternatively, release the read lock in batches or stream the response incrementally rather than collecting the entire result before returning.

## Proof of Concept
1. Configure a node with `max_tx_pool_size = 180_000_000`.
2. Submit a large number of minimum-size valid transactions (paying `min_fee_rate = 1000` shannons/KB) until the pool is near capacity.
3. Call `{"method":"get_raw_tx_pool","params":[true]}` via JSON-RPC.
4. Observe: heap allocation spikes proportional to pool entry count × `TxEntryInfo` size; the tx-pool read lock is held for the full duration; concurrent `send_transaction` and `get_block_template` calls stall; under sufficient pool load the node process is killed by the OOM killer.