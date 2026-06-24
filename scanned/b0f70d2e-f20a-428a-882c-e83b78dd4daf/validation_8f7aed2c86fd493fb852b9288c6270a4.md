Audit Report

## Title
Missing Per-Peer Rate Limiting on `GetBlockFilters` Allows Unbounded DB-Read Amplification — (`sync/src/filter/get_block_filters_process.rs`)

## Summary
The `BlockFilter` P2P protocol handler processes `GetBlockFilters`, `GetBlockFilterHashes`, and `GetBlockFilterCheckPoints` requests from any peer without per-peer rate limiting. A single unprivileged remote peer can send `GetBlockFilters` in a tight loop, forcing the node to perform up to 2000 synchronous RocksDB reads and transmit a large response per iteration, with no throttle or backpressure applied. This is a concrete design gap relative to every other P2P handler in the codebase.

## Finding Description
`GetBlockFiltersProcess::execute` iterates up to `BATCH_SIZE = 1000` blocks per request. For each block it calls `active_chain.get_block_hash(block_number)` and `active_chain.get_block_filter(&block_hash)` — two synchronous DB reads — then builds and sends a `BlockFilters` response. [1](#0-0) [2](#0-1) 

The only guard is a 1.8 MB encoded-size cap. For blocks with small or empty filters (early mainnet blocks, low-traffic chain segments), this cap is never reached, so all 1000 entries are returned every time. [3](#0-2) 

The `BlockFilter` handler struct carries no rate-limiter state and `try_process` performs no rate check before dispatching any of the three request message types: [4](#0-3) [5](#0-4) 

By contrast, `Relayer` explicitly carries a `rate_limiter: RateLimiter<(PeerIndex, u32)>` and enforces it at the top of `try_process` before any dispatch (30 req/s per peer+message-type): [6](#0-5) [7](#0-6) 

`HolePunching` similarly enforces both a `rate_limiter` and `forward_rate_limiter` before processing any message: [8](#0-7) [9](#0-8) 

A grep for `rate_limit` or `RateLimiter` under `sync/src/filter/` returns zero matches, confirming the Filter protocol is the only production P2P handler entirely missing this protection.

## Impact Explanation
Each `GetBlockFilters` request from a single peer causes up to 2000 RocksDB reads (1000 × `get_block_hash` + 1000 × `get_block_filter`) and one large async send (up to ~1.8 MB) queued to the peer. A peer sending requests in a tight loop — limited only by network RTT — can sustain hundreds of such cycles per second, saturating the node's DB read bandwidth and async send queue. This degrades response latency for all other peers across all protocols (sync, relay, filter), constituting a **High** impact: a bad design that can cause CKB network congestion with few costs, as a single standard P2P connection is sufficient to mount the attack.

## Likelihood Explanation
The attack requires only a standard P2P connection — no special privileges, no PoW, no keys. The attacker controls only `start_number` (a `u64`), which is fully attacker-controlled and not validated beyond checking `latest >= start_number`. [10](#0-9) 

The attack is locally testable and reproducible with a single peer against any CKB full node with block filter enabled and filter data built for ≥1000 blocks.

## Recommendation
Add a `RateLimiter<(PeerIndex, u32)>` field to `BlockFilter` (mirroring `Relayer`) and check it at the top of `try_process` before dispatching `GetBlockFilters`, `GetBlockFilterHashes`, and `GetBlockFilterCheckPoints`. A quota of 10–30 req/s per peer per message type is consistent with the existing policy in `Relayer`. Call `rate_limiter.retain_recent()` in the `disconnected` handler, as done in `Relayer`. [11](#0-10) 

## Proof of Concept
1. Connect a single peer to a CKB full node with block filter enabled and filter data built for ≥1000 blocks with small filters (e.g., `start_number: 0` on mainnet or a local devnet with empty blocks).
2. In a tight loop, send `GetBlockFilters { start_number: 0 }` over the Filter protocol.
3. Observe: the node performs 2000 DB reads and sends a response per iteration, with no throttling applied.
4. Simultaneously measure response latency for a second peer issuing normal sync requests — latency degrades proportionally to the request rate of the attacking peer. [12](#0-11)

### Citations

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** sync/src/filter/get_block_filters_process.rs (L33-85)
```rust
    pub async fn execute(self) -> Status {
        let active_chain = self.filter.shared.active_chain();
        let start_number: BlockNumber = self.message.to_entity().start_number().into();
        let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();

        if latest >= start_number {
            let mut block_hashes = Vec::new();
            let mut filters = Vec::new();
            let mut current_content_size = 0;
            current_content_size += 8; // Size of start_number
            current_content_size += 4 * 2; // Size of the header field `full-size` of `block_hash` and `block_filter`
            let mut block_number = start_number;
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
                let Some(next_block_number) = block_number.checked_add(1) else {
                    break;
                };
                block_number = next_block_number;
            }
            let content = packed::BlockFilters::new_builder()
                .start_number(start_number)
                .block_hashes(block_hashes)
                .filters(filters)
                .build();
            let message = packed::BlockFilterMessage::new_builder()
                .set(content)
                .build();
            async_send_message_to(&self.nc, self.peer, &message).await
        } else {
            Status::ignored()
        }
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

**File:** sync/src/relayer/mod.rs (L81-81)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
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
