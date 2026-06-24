Audit Report

## Title
Missing Per-Peer Rate Limit in BlockFilter Protocol Enables Unbounded RocksDB Read Amplification — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filter_hashes_process.rs`)

## Summary
The `BlockFilter` protocol handler has no per-peer rate limiter, while the `Relayer` protocol explicitly uses a `governor`-based limiter at 30 req/sec. A single unprivileged peer can send minimal `GetBlockFilterHashes` or `GetBlockFilterCheckPoints` messages in a tight loop, each triggering up to 4,000 synchronous RocksDB point reads, saturating the node's shared storage I/O path and degrading throughput for all concurrent operations.

## Finding Description
`GetBlockFilterHashesProcess::execute()` iterates up to `BATCH_SIZE=2000` times, performing two DB reads per iteration (`get_block_hash` + `get_block_filter_hash`), for a maximum of 4,000 reads per message: [1](#0-0) [2](#0-1) 

`GetBlockFilterCheckPointsProcess::execute()` has the same structure with `BATCH_SIZE=2000` and `CHECK_POINT_INTERVAL=2000`: [3](#0-2) [4](#0-3) 

The `BlockFilter` struct contains no rate limiter field: [5](#0-4) 

The `received` method dispatches directly to `process` → `try_process` with no rate check at any point: [6](#0-5) 

By contrast, `Relayer` holds a `RateLimiter<(PeerIndex, u32)>` field and checks it at the top of `try_process` before any handler is invoked: [7](#0-6) [8](#0-7) [9](#0-8) 

The `Filter` protocol is enabled by default and registered unconditionally when `SupportProtocol::Filter` is in the support set: [10](#0-9) 

The 2 MB `max_frame_length` governs message size, not message rate, so a tiny `GetBlockFilterHashes` message (a single `Uint64` field, ~8 bytes encoded) is well within the limit and can be sent at the maximum TCP/frame rate with no server-side throttle.

## Impact Explanation
A single attacker peer can drive up to 4,000 synchronous RocksDB point reads per message with no rate constraint. Sustained at TCP line rate, this saturates the shared RocksDB read path, increasing latency for block sync (`Synchronizer`) and transaction relay (`Relayer`) for all honest peers. This constitutes a bad design that can cause CKB network congestion with few costs (a standard P2P connection is the only prerequisite), matching the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs*.

## Likelihood Explanation
The attack requires only a standard P2P connection to a node with the Filter protocol enabled (the default configuration). No proof-of-work, no keys, no privileged access are needed. The input-to-work asymmetry (~8 bytes in → 4,000 DB reads) is extreme, locally testable, and repeatable indefinitely. Multiple attackers connecting simultaneously multiply the effect linearly.

## Recommendation
Add a per-peer rate limiter to `BlockFilter` mirroring the pattern in `Relayer`:
- Add a `RateLimiter<(PeerIndex, u32)>` field to the `BlockFilter` struct (mirroring `sync/src/relayer/mod.rs` lines 63–67 and 81)
- Initialize it in `BlockFilter::new` with an appropriate quota (e.g., 10 req/sec per peer per message type)
- Check the rate limit at the top of `try_process` before dispatching to any `GetBlock*` handler, returning `StatusCode::TooManyRequests` (and optionally banning) on violation

## Proof of Concept
1. Connect to a CKB node with the Filter protocol enabled (default config)
2. In a tight loop, send `packed::GetBlockFilterHashes::new_builder().start_number(0u64.pack()).build()` wrapped in a `BlockFilterMessage`
3. Monitor RocksDB read IOPS on the server (e.g., via `rocksdb.block.cache.miss` metrics or OS-level iostat); expect up to 4,000 reads per message × message rate
4. Simultaneously measure response latency on the Sync/Relay protocols for honest peers and observe degradation
5. Repeat with `GetBlockFilterCheckPoints` to confirm the same amplification via `get_block_filter_check_points_process.rs`

### Citations

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L53-66)
```rust
            for _ in 0..BATCH_SIZE {
                if let Some(block_filter_hash) = active_chain
                    .get_block_hash(block_number)
                    .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
                {
                    block_filter_hashes.push(block_filter_hash);
                } else {
                    break;
                }
                let Some(next_block_number) = block_number.checked_add(1) else {
                    break;
                };
                block_number = next_block_number;
            }
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-10)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L43-56)
```rust
            for _ in 0..BATCH_SIZE {
                if let Some(block_filter_hash) = active_chain
                    .get_block_hash(block_number)
                    .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
                {
                    block_filter_hashes.push(block_filter_hash);
                } else {
                    break;
                }
                let Some(next_block_number) = block_number.checked_add(CHECK_POINT_INTERVAL) else {
                    break;
                };
                block_number = next_block_number;
            }
```

**File:** sync/src/filter/mod.rs (L22-25)
```rust
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L122-153)
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
        debug_target!(
```

**File:** sync/src/relayer/mod.rs (L63-67)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;
```

**File:** sync/src/relayer/mod.rs (L81-81)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
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

**File:** util/launcher/src/lib.rs (L443-456)
```rust
        if support_protocols.contains(&SupportProtocol::Filter) {
            let filter = BlockFilter::new(Arc::clone(&sync_shared));

            protocols.push(
                CKBProtocol::new_with_support_protocol(
                    SupportProtocols::Filter,
                    Box::new(filter),
                    Arc::clone(&network_state),
                )
                .compress(false),
            );
        } else {
            flags.remove(Flags::BLOCK_FILTER);
        }
```
