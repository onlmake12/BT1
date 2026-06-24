All code references check out against the actual source. The finding is valid.

---

Audit Report

## Title
Unbounded Per-Message DB Read Loop in `GetBlockFilterHashesProcess::execute` Enables Resource Exhaustion via Filter Protocol Flooding — (`sync/src/filter/get_block_filter_hashes_process.rs`)

## Summary

`GetBlockFilterHashesProcess::execute` performs up to 4,000 synchronous RocksDB reads per received `GetBlockFilterHashes` message (2,000 loop iterations × 2 reads each). The Filter protocol handler dispatches directly to this function with no per-peer rate limiting, token bucket, or quota of any kind, while the Relay protocol handler has an equivalent `governor::RateLimiter`. An unprivileged remote peer can flood the node with minimal-cost messages, forcing proportional DB read work and degrading sync throughput for all connected peers.

## Finding Description

`BATCH_SIZE` is hardcoded to 2000: [1](#0-0) 

The main loop runs up to 2,000 iterations, each calling `get_block_hash` and `get_block_filter_hash` — two synchronous RocksDB reads per iteration, totalling up to 4,000 reads per message: [2](#0-1) 

When `start_number = 0`, the pre-loop branch short-circuits to `packed::Byte32::zero()` (no DB read), but the main loop still executes fully for up to 2,000 iterations: [3](#0-2) 

The `received` handler in `sync/src/filter/mod.rs` dispatches directly to `process` → `try_process` → `execute` with no rate check, no token bucket, and no quota at any layer: [4](#0-3) 

By contrast, the Relay protocol defines a `governor::RateLimiter` keyed per `(PeerIndex, message_type)` and checks it at the top of `try_process` before any message handling: [5](#0-4) [6](#0-5) 

The `BlockFilter` struct holds no `rate_limiter` field and no rate-check logic exists anywhere in `sync/src/filter/`: [7](#0-6) 

## Impact Explanation

Each `GetBlockFilterHashes { start_number: 0 }` message on a chain with ≥2,000 built filter blocks triggers exactly 4,000 synchronous RocksDB reads. A single peer sending these messages in a tight loop forces proportional DB read IOPS, saturating read bandwidth and starving the async executor. This degrades sync throughput for all connected peers and can cause CKB network congestion with negligible attacker cost. This matches the allowed **High** impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation

The attacker requires only a standard TCP connection to a CKB node with the Filter protocol enabled (the default). No PoW, stake, key material, or special privileges are needed. The message payload is 8 bytes (`start_number` as a `Uint64`). The cost to the attacker is negligible; the cost to the victim scales linearly with chain length and message rate. The condition `latest >= start_number` is trivially satisfied when `start_number = 0` on any non-empty chain.

## Recommendation

1. Add a per-peer rate limiter (token bucket via `governor`) to the `BlockFilter` handler, mirroring the `RateLimiter<(PeerIndex, u32)>` already present in `sync/src/relayer/mod.rs`.
2. Check the rate limit at the top of `BlockFilter::try_process` before dispatching to any process handler, consistent with the pattern in `Relayer::try_process`.
3. Optionally enforce a minimum interval between responses to the same peer for the same message type, or deduplicate repeated identical requests within a short window.
4. Consider banning peers that exceed the rate limit, consistent with the existing `BAD_MESSAGE_BAN_TIME` pattern already used in `sync/src/filter/mod.rs`.

## Proof of Concept

1. Spin up a CKB node with ≥2,000 blocks and block filters built (Filter protocol enabled by default).
2. Connect a custom peer that sends `GetBlockFilterHashes { start_number: 0 }` in a tight loop (e.g., 100 messages/second).
3. Monitor RocksDB read IOPS via metrics or `perf stat` — they spike at 400,000 reads/second per attacker peer.
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

**File:** sync/src/filter/mod.rs (L22-25)
```rust
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
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

**File:** sync/src/relayer/mod.rs (L116-123)
```rust
        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```
