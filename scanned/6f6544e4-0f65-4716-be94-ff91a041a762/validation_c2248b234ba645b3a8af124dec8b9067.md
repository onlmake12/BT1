Audit Report

## Title
Missing Per-Peer Rate Limiting in `BlockFilter` Protocol Enables I/O Exhaustion via Cheap `GetBlockFilters` Flood — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filters_process.rs`)

## Summary
The `BlockFilter` protocol handler contains no per-peer rate limiter. A single attacker peer can send a continuous stream of well-formed, ~12-byte `GetBlockFilters` messages, each of which causes the server to execute up to 2,000 sequential RocksDB reads (up to 1,000 × `get_block_hash` + `get_block_filter`). No throttle or ban is applied for this pattern, while the `Relayer` and `HolePunching` protocols both enforce explicit `governor`-based rate limits of 30 req/sec per peer.

## Finding Description
The `BlockFilter` struct holds only `shared: Arc<SyncShared>` with no `rate_limiter` field: [1](#0-0) 

The `received()` handler bans only unparseable messages; any well-formed `GetBlockFilters` with any `start_number` passes directly to `execute()` with zero throttling: [2](#0-1) 

`GetBlockFiltersProcess::execute()` loops up to `BATCH_SIZE = 1000` iterations, calling `get_block_hash` and `get_block_filter` on each — up to 2,000 DB reads per message: [3](#0-2) [4](#0-3) 

By contrast, `Relayer` carries an explicit `rate_limiter: RateLimiter<(PeerIndex, u32)>` and checks it before any dispatch: [5](#0-4) [6](#0-5) 

There is no equivalent guard anywhere in `sync/src/filter/`.

## Impact Explanation
Each ~12-byte `GetBlockFilters` message forces the full node to execute up to 2,000 RocksDB point-reads. An attacker maintaining a single TCP connection can sustain a high-frequency flood of such messages, saturating the node's I/O and async executor threads. This degrades or halts block/header sync for all legitimate peers sharing the same node. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation
The attack requires only a standard P2P connection to a full node with the Filter protocol enabled. No key material, privileged role, or hashpower is needed. The `GetBlockFilters` message is trivially constructable (a single `Uint64` field). The absence of any rate-limiting guard makes the path fully reliable and repeatable by any unprivileged peer.

## Recommendation
Add a `governor`-based `RateLimiter<(PeerIndex, u32)>` field to the `BlockFilter` struct, mirroring the implementation in `Relayer`: [7](#0-6) 

Check the rate limiter in `received()` (or `try_process()`) before dispatching to `GetBlockFiltersProcess::execute()`, using a quota consistent with the existing policy (e.g., 30 `GetBlockFilters` requests per second per peer). Call `rate_limiter.retain_recent()` in the `disconnected()` handler to prevent unbounded memory growth.

## Proof of Concept
```rust
// Attacker loop (pseudocode):
loop {
    let msg = packed::GetBlockFilters::new_builder()
        .start_number(0u64.pack())
        .build();
    let envelope = packed::BlockFilterMessage::new_builder().set(msg).build();
    // ~12 bytes on the wire; no server-side throttle will stop this
    send_to_victim(SupportProtocols::Filter, envelope.as_bytes());
    // Each iteration forces the victim to perform up to 2,000 RocksDB reads.
    // No ban is ever triggered for well-formed messages.
}
```
To reproduce: connect to a node with Filter enabled, send `GetBlockFilters { start_number: 0 }` in a tight loop from a single peer, and observe I/O saturation and stalled sync for other peers via node metrics or peer latency monitoring.

### Citations

**File:** sync/src/filter/mod.rs (L21-25)
```rust
#[derive(Clone)]
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L128-143)
```rust
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
```

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** sync/src/filter/get_block_filters_process.rs (L45-67)
```rust
            for _ in 0..BATCH_SIZE {
                if let Some(block_hash) = active_chain.get_block_hash(block_number) {
                    if let Some(block_filter) = active_chain.get_block_filter(&block_hash) {
                        if current_content_size
                            + block_hash.as_slice().len()
                            + 4
                            + block_filter.as_slice().len()
                            + 4
                            >= (1.8 * 1024.0 * 1024.0) as usize
                        {
                            // Break if the encoded size of `block_hash` + `block_filter` + `start_number` + molecule header increase reaches 1.8MB, to avoid frame size too large
                            break;
                        }
                        current_content_size +=
                            block_hash.as_slice().len() + block_filter.as_slice().len() + 4;
                        block_hashes.push(block_hash);
                        filters.push(block_filter);
                    } else {
                        break;
                    }
                } else {
                    break;
                }
```

**File:** sync/src/relayer/mod.rs (L81-82)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
```

**File:** sync/src/relayer/mod.rs (L88-98)
```rust
    pub fn new(chain: ChainController, shared: Arc<SyncShared>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        Relayer {
            chain,
            shared,
            rate_limiter,
        }
```

**File:** sync/src/relayer/mod.rs (L113-123)
```rust
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));

        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```
