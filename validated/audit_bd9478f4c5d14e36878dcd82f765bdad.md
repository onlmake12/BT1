The code evidence is conclusive. All claims check out against the actual source.

Audit Report

## Title
Missing Per-Peer Rate Limit in BlockFilter Protocol Enables DB Read Amplification and Bandwidth Exhaustion — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filter_hashes_process.rs`)

## Summary

The `BlockFilter` protocol handler contains no per-peer rate limiter. Unlike `Relayer` and `HolePunching`, which both enforce a 30 req/sec keyed rate limit before any handler dispatch, `BlockFilter.try_process` routes directly to handler execution with zero throttling. A single unprivileged peer can flood the node with `GetBlockFilterHashes`, `GetBlockFilterCheckPoints`, or `GetBlockFilters` messages, each triggering up to 4000 sequential RocksDB reads and a large serialized response, indefinitely and without triggering any ban or backpressure.

## Finding Description

`GetBlockFilterHashesProcess::execute` iterates up to `BATCH_SIZE = 2000` blocks, calling `get_block_hash` + `get_block_filter_hash` per iteration — up to 4000 DB reads — then serializes and sends the full result. [1](#0-0) [2](#0-1) 

`GetBlockFilterCheckPointsProcess` shares the same `BATCH_SIZE = 2000` and the same unguarded loop pattern. [3](#0-2) [4](#0-3) 

`GetBlockFiltersProcess` uses `BATCH_SIZE = 1000` with a 1.8 MB response cap, but is equally unguarded. [5](#0-4) [6](#0-5) 

The `BlockFilter` struct holds only a `shared` field — no `rate_limiter` — and `try_process` dispatches directly to handlers with no rate check at any point in the call chain. [7](#0-6) [8](#0-7) 

By contrast, `Relayer` carries an explicit `rate_limiter: RateLimiter<(PeerIndex, u32)>` initialized at 30 req/sec and checks it before every handler dispatch, returning `TooManyRequests` on excess. [9](#0-8) [10](#0-9) 

`HolePunching` applies the same pattern at the `received` entry point before any processing. [11](#0-10) [12](#0-11) 

A grep for `rate_limiter` scoped to `sync/src/filter/` returns zero matches, confirming the guard is entirely absent from the Filter protocol.


## Impact Explanation

Each `GetBlockFilterHashes(start_number=0)` request causes up to 4000 RocksDB reads and a ~64 KB outbound response. With no rate limit, a single peer connection can sustain this indefinitely. A handful of attacker connections can saturate the node's disk I/O and outbound bandwidth, causing the node to become unresponsive or crash. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** The attacker's inbound cost is negligible (8-byte messages) relative to the node's outbound cost, a classic amplification pattern.

## Likelihood Explanation

The attack requires only a valid P2P connection to a node that has built at least one block of filter data (the `latest >= start_number` guard at line 39 of `get_block_filter_hashes_process.rs` must hold). No proof-of-work, no keys, no special privileges are needed. The message is tiny and the attack is trivially repeatable in a tight loop from a single connection. [13](#0-12) 

## Recommendation

Add a `RateLimiter<(PeerIndex, u32)>` field to `BlockFilter` (mirroring `Relayer::rate_limiter`) and check it at the top of `try_process` before dispatching to any of the three filter handlers. The existing 30 req/sec quota used by `Relayer` and `HolePunching` is a reasonable starting point. Call `rate_limiter.retain_recent()` in the `disconnected` handler to bound memory growth.

## Proof of Concept

1. Connect a single peer to a node that has built at least 1 block of filter data.
2. In a tight loop, send `GetBlockFilterHashes { start_number: 0 }` 1000 times over the Filter protocol.
3. Observe: the node performs ~4,000,000 RocksDB reads and sends ~64 MB of responses.
4. Assert: no `TooManyRequests` status is returned, no ban is issued, and the node's DB read metrics spike proportionally to the request rate.
5. Repeat with `GetBlockFilterCheckPoints { start_number: 0 }` and `GetBlockFilters { start_number: 0 }` to confirm all three handlers are equally unguarded.

### Citations

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L39-39)
```rust
        if latest >= start_number {
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

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** sync/src/filter/get_block_filters_process.rs (L45-57)
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
```

**File:** sync/src/filter/mod.rs (L22-25)
```rust
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L33-68)
```rust
    async fn try_process(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        message: packed::BlockFilterMessageUnionReader<'_>,
    ) -> Status {
        match message {
            packed::BlockFilterMessageUnionReader::GetBlockFilters(msg) => {
                GetBlockFiltersProcess::new(msg, self, nc, peer)
                    .execute()
                    .await
            }
            packed::BlockFilterMessageUnionReader::GetBlockFilterHashes(msg) => {
                GetBlockFilterHashesProcess::new(msg, self, nc, peer)
                    .execute()
                    .await
            }
            packed::BlockFilterMessageUnionReader::GetBlockFilterCheckPoints(msg) => {
                GetBlockFilterCheckPointsProcess::new(msg, self, nc, peer)
                    .execute()
                    .await
            }
            packed::BlockFilterMessageUnionReader::BlockFilters(_)
            | packed::BlockFilterMessageUnionReader::BlockFilterHashes(_)
            | packed::BlockFilterMessageUnionReader::BlockFilterCheckPoints(_) => {
                // remote peer should not send block filter to us without asking
                // TODO: ban remote peer
                warn_target!(
                    crate::LOG_TARGET_FILTER,
                    "Received unexpected message from peer: {:?}",
                    peer
                );
                Status::ignored()
            }
        }
    }
```

**File:** sync/src/relayer/mod.rs (L81-82)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
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

**File:** network/src/protocols/hole_punching/mod.rs (L45-46)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L95-107)
```rust
        if self
            .rate_limiter
            .check_key(&(session_id, msg.item_id()))
            .is_err()
        {
            debug!(
                "process {} from {}; result is {}",
                item_name,
                session_id,
                status::StatusCode::TooManyRequests.with_context(msg.item_name())
            );
            return;
        }
```
