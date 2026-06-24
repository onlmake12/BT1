Audit Report

## Title
Missing Per-Peer Rate Limit on `GetBlockFilters` Allows Bandwidth Exhaustion — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filters_process.rs`)

## Summary
The `BlockFilter` P2P protocol handler contains no per-peer rate limiting on `GetBlockFilters` messages. A single unauthenticated remote peer can flood the node with `GetBlockFilters` requests, each triggering up to 1000 ChainDB reads and a ~1.8 MB response, with no throttle. This can saturate the node's outbound bandwidth and starve legitimate peers.

## Finding Description
`GetBlockFiltersProcess::execute` loops up to `BATCH_SIZE = 1000` times, calling `active_chain.get_block_hash(block_number)` and `active_chain.get_block_filter(&block_hash)` per iteration, then serializes and sends a response capped at ~1.8 MB via `async_send_message_to`. [1](#0-0) [2](#0-1) 

The `BlockFilter` struct holds only `shared: Arc<SyncShared>` — no rate limiter field exists: [3](#0-2) 

The `received` handler dispatches directly to `self.process(...)` with no rate-limit check before or after parsing: [4](#0-3) 

By contrast, `Relayer` explicitly constructs a `RateLimiter<(PeerIndex, u32)>` at 30 req/s per peer+message type and gates every non-PoW message through it in `try_process`: [5](#0-4) [6](#0-5) [7](#0-6) 

The 1.8 MB response cap prevents frame-level disconnects but does not limit request frequency in any way.

## Impact Explanation
A single unauthenticated peer can monopolize the node's outbound bandwidth. At high request rates, the node attempts to send large responses continuously to that one peer, saturating the send queue and starving legitimate peers (other light clients and full nodes). This matches the allowed CKB bounty impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"** (High, 10001–15000 points). The attacker needs only a valid P2P connection and the ability to send well-formed messages at high frequency — no authentication, PoW, or special privilege is required.

## Likelihood Explanation
The attack requires only establishing a standard P2P connection and sending `GetBlockFilters { start_number: 0 }` in a tight loop. The `GetBlockFilters` message is a single `Uint64` field, trivially constructable. No special knowledge, credentials, or chain state manipulation is needed. The attack is repeatable indefinitely and can be automated trivially.

## Recommendation
Add a per-peer rate limiter to `BlockFilter`, mirroring the pattern in `Relayer`:
- Add a `RateLimiter<(PeerIndex, u8)>` field to the `BlockFilter` struct (analogous to `Relayer`'s `rate_limiter` field at `sync/src/relayer/mod.rs:81`).
- In `try_process` (`sync/src/filter/mod.rs:33`), before dispatching `GetBlockFilters` and `GetBlockFilterHashes`, call `rate_limiter.check_key(&(peer, message.item_id()))` and return `StatusCode::TooManyRequests` on failure.
- A quota of ~10 req/s per peer per message type is consistent with the existing relay policy (which uses 30 req/s as a hard cap with buffer for higher-frequency relay messages).

## Proof of Concept
1. Run a CKB node with ≥1000 blocks that have filters built.
2. Connect a single peer using the Filter protocol.
3. In a tight loop, send `GetBlockFilters { start_number: 0 }` over the Filter protocol connection.
4. Measure bytes received per second from the node on that connection — on localhost, this will exceed any reasonable per-peer bandwidth cap (e.g., >10 MB/s is achievable).
5. Simultaneously connect a second legitimate peer and observe that it receives degraded or no responses during the flood, confirming resource starvation.

### Citations

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

**File:** sync/src/filter/mod.rs (L21-25)
```rust
#[derive(Clone)]
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

**File:** sync/src/relayer/mod.rs (L89-98)
```rust
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

**File:** sync/src/relayer/mod.rs (L112-123)
```rust
        // CompactBlock will be verified by POW, it's OK to skip rate limit checking.
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
