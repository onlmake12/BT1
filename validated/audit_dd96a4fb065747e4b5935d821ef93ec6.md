Based on my investigation of the codebase, here is my analysis:

---

### Title
Missing Per-Peer Rate Limit in BlockFilter Protocol Allows Unbounded RocksDB Read Amplification — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filter_hashes_process.rs`)

### Summary

An unprivileged remote peer can connect via the Filter protocol (enabled by default) and repeatedly send `GetBlockFilterHashes{start_number:0}` messages. Each tiny (~8-byte) message triggers up to **4,000 RocksDB point reads** (2,000 `get_block_hash` + 2,000 `get_block_filter_hash`) with no per-peer rate limit, allowing a single attacker to saturate the node's storage I/O path.

### Finding Description

**The handler** in `GetBlockFilterHashesProcess::execute()` iterates up to `BATCH_SIZE=2000` times, performing two DB reads per iteration: [1](#0-0) [2](#0-1) 

The incoming `GetBlockFilterHashes` message is a fixed-size struct containing only a `Uint64` start_number — approximately 8 bytes of attacker-controlled input triggers up to 4,000 RocksDB reads on the server side.

**The `BlockFilter` handler has no rate limiter.** The `BlockFilter` struct contains only `shared: Arc<SyncShared>`: [3](#0-2) 

The `received` method dispatches directly to `try_process` with no rate check: [4](#0-3) 

**Contrast with the Relayer**, which explicitly uses a `governor`-based rate limiter keyed by `(PeerIndex, message_type)` at 30 req/sec: [5](#0-4) [6](#0-5) [7](#0-6) 

The Filter protocol's `max_frame_length` is 2MB: [8](#0-7) 

This limit governs the *size* of individual frames, not the *rate* of incoming requests. A tiny 8-byte `GetBlockFilterHashes` message is well within this limit and can be sent at the maximum TCP/frame rate.

**The Filter protocol is enabled by default** and registered in `start_network_and_rpc`: [9](#0-8) 

### Impact Explanation

A single attacker peer can send `GetBlockFilterHashes{start_number:0}` at the maximum frame rate. Each message causes the node to perform up to 4,000 synchronous RocksDB point reads before returning. This saturates the shared RocksDB read path, increasing latency for all concurrent operations including block sync (`Synchronizer`) and transaction relay (`Relayer`), degrading honest peer throughput.

### Likelihood Explanation

The attack requires only a standard P2P connection to a node with the Filter protocol enabled (the default). No PoW, no keys, no privileged access. The asymmetry (8 bytes in → 4,000 DB reads) is extreme and locally testable.

### Recommendation

Add a per-peer rate limiter to `BlockFilter` mirroring the pattern already used in `Relayer`:
- Add a `RateLimiter<(PeerIndex, u32)>` field to the `BlockFilter` struct
- Check the rate limit at the top of `try_process` before dispatching to any `GetBlock*` handler
- Return `StatusCode::TooManyRequests` (and optionally disconnect/ban) on violation

### Proof of Concept

1. Connect to a CKB node with Filter protocol enabled (default config)
2. In a tight loop, send `packed::GetBlockFilterHashes::new_builder().start_number(0u64).build()` wrapped in `BlockFilterMessage`
3. Observe RocksDB read IOPS spike (up to 4,000 reads per message × message rate)
4. Measure increased latency on the Sync/Relay protocols for honest peers

The `GetBlockFilterCheckPointsProcess` has the same issue with `BATCH_SIZE=2000` and `CHECK_POINT_INTERVAL=2000`: [10](#0-9) [11](#0-10)

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

**File:** sync/src/relayer/mod.rs (L89-92)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
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

**File:** network/src/protocols/support_protocols.rs (L134-134)
```rust
            SupportProtocols::Filter => 2 * 1024 * 1024,      // 2   MB
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
