Audit Report

## Title
Unbounded Per-Message DB Read Loop in `GetBlockFilterHashesProcess::execute` Enables Resource Exhaustion via Filter Protocol Flooding — (`sync/src/filter/get_block_filter_hashes_process.rs`)

## Summary

`GetBlockFilterHashesProcess::execute` loops up to `BATCH_SIZE = 2000` times, performing two RocksDB reads per iteration (up to 4,000 reads per message). The filter protocol handler dispatches directly to this function with no per-peer rate limiting, token bucket, or quota, while the relay protocol does have a `governor::RateLimiter`. An unprivileged remote peer can flood the node with `GetBlockFilterHashes { start_number: 0 }` messages at negligible cost, forcing unbounded DB read work and degrading sync throughput for all connected peers.

## Finding Description

`BATCH_SIZE` is set to 2000: [1](#0-0) 

The main loop runs up to 2000 iterations, each calling `get_block_hash` and `get_block_filter_hash` — two synchronous RocksDB reads: [2](#0-1) 

The guard `latest >= start_number` is trivially satisfied when `start_number = 0`, since `latest` is always `>= 0`. When `start_number = 0`, the pre-loop branch short-circuits to `packed::Byte32::zero()` without any DB read, but the main loop still executes fully: [3](#0-2) 

The `received` handler in `sync/src/filter/mod.rs` dispatches directly to `process` → `try_process` → `execute` with no rate check, no token bucket, and no quota at any layer: [4](#0-3) 

By contrast, the relay protocol defines a `governor::RateLimiter` keyed per peer: [5](#0-4) 

A grep across `sync/src/**/*.rs` for `rate_limit`, `RateLimiter`, `throttle`, and `quota` returns zero matches in any filter file — only `sync/src/relayer/mod.rs` has matches.

## Impact Explanation

Each `GetBlockFilterHashes { start_number: 0 }` message on a chain with ≥2000 built filter blocks triggers exactly 4,000 synchronous RocksDB reads. A single peer sending these messages in a tight loop forces proportional DB read IOPS, saturating read bandwidth and starving the async executor. This degrades sync throughput for all connected peers and can cause CKB network congestion with negligible attacker cost. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation

The attacker requires only a standard TCP connection to a CKB node with the Filter protocol enabled (the default). No PoW, stake, key material, or special privileges are needed. The message payload is 8 bytes (`start_number` as a `Uint64`). The cost to the attacker is negligible; the cost to the victim scales linearly with chain length and message rate.

## Recommendation

1. Add a per-peer rate limiter (token bucket via `governor`) to the Filter protocol handler, mirroring the `RateLimiter` already present in `sync/src/relayer/mod.rs`.
2. Optionally enforce a minimum interval between responses to the same peer for the same message type, or deduplicate repeated identical requests.
3. Consider banning peers that send repeated identical `GetBlockFilterHashes` requests within a short window, consistent with the existing `BAD_MESSAGE_BAN_TIME` pattern used elsewhere in `sync/src/filter/mod.rs`.

## Proof of Concept

1. Spin up a CKB node with ≥2000 blocks and block filters built (Filter protocol enabled by default).
2. Connect a custom peer that sends `GetBlockFilterHashes { start_number: 0 }` in a tight loop (e.g., 100 messages/second).
3. Monitor RocksDB read IOPS via metrics or `perf stat` — they spike at 400,000 reads per second per attacker peer.
4. Simultaneously connect a legitimate sync peer and measure its sync throughput — it degrades as the async executor is occupied serving the flood.
5. Disconnect the attacker peer and observe throughput recovery, confirming the causal link.

### Citations

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L39-50)
```rust
        if latest >= start_number {
            let parent_block_filter_hash = if start_number > 0 {
                match active_chain
                    .get_block_hash(start_number - 1)
                    .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
                {
                    Some(parent_block_filter_hash) => parent_block_filter_hash,
                    None => return Status::ignored(),
                }
            } else {
                packed::Byte32::zero()
            };
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L53-56)
```rust
            for _ in 0..BATCH_SIZE {
                if let Some(block_filter_hash) = active_chain
                    .get_block_hash(block_number)
                    .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
```

**File:** sync/src/filter/mod.rs (L122-152)
```rust
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        data: Bytes,
    ) {
        let msg = match packed::BlockFilterMessageReader::from_compatible_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                info_target!(
                    crate::LOG_TARGET_FILTER,
                    "Peer {} sends us a malformed message",
                    peer_index
                );
                nc.ban_peer(
                    peer_index,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        debug_target!(
            crate::LOG_TARGET_FILTER,
            "received msg {} from {}",
            msg.item_name(),
            peer_index
        );
        let start_time = Instant::now();
        self.process(nc, peer_index, msg).await;
```

**File:** sync/src/relayer/mod.rs (L63-67)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;
```
