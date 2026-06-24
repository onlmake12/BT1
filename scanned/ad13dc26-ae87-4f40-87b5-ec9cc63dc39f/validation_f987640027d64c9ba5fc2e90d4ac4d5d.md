Audit Report

## Title
Missing Per-Peer Rate Limiter in `BlockFilter` Protocol Enables Unbounded DB Read Amplification — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filter_hashes_process.rs`)

## Summary

The `BlockFilter` P2P protocol handler contains no rate limiter, unlike `Relayer` and `HolePunching` which both carry a `governor`-based `RateLimiter<(PeerIndex, u32)>`. Any unauthenticated peer can flood `GetBlockFilterHashes` (or `GetBlockFilters` / `GetBlockFilterCheckPoints`) messages at wire speed, triggering up to 4,001 RocksDB reads per message with no throttle, cooldown, or per-peer budget. This can saturate the storage layer and cause CKB network congestion.

## Finding Description

`BlockFilter` is defined with a single field and no rate-limiter:

```rust
pub struct BlockFilter {
    shared: Arc<SyncShared>,
}
``` [1](#0-0) 

The `received` entry point parses the message and immediately dispatches to `process` → `try_process` → the relevant `Process::execute` with zero admission control: [2](#0-1) 

Inside `GetBlockFilterHashesProcess::execute`, when `latest >= start_number`, the handler performs:
1. One `get_block_hash(start_number - 1)` + `get_block_filter_hash` for the parent (2 DB reads).
2. A loop of up to `BATCH_SIZE = 2000` iterations, each doing `get_block_hash` + `get_block_filter_hash` (up to 4,000 more DB reads). [3](#0-2) [4](#0-3) 

The short-circuit only fires when `start_number > latest`: [5](#0-4) 

`GetBlockFiltersProcess` (BATCH_SIZE=1000, up to 2,000 reads) and `GetBlockFilterCheckPointsProcess` (BATCH_SIZE=2000, up to 4,000 reads) are equally unprotected: [6](#0-5) [7](#0-6) 

By contrast, `Relayer` carries a `rate_limiter` field and gates every non-PoW message before dispatch: [8](#0-7) [9](#0-8) 

`HolePunching` has both a per-peer and a per-route rate limiter checked before any processing: [10](#0-9) [11](#0-10) 

`BlockFilter` has neither. [12](#0-11) 

## Impact Explanation

A single attacker peer sending `GetBlockFilterHashes` with `start_number=0` at maximum rate forces the victim node to execute up to **4,001 RocksDB reads per message** with no bound. At even modest message rates (e.g., 1,000 msg/s), this is ~4 million DB reads per second from one peer. Multiple peers compound linearly. This saturates the storage layer, starving block validation, sync, and relay processing.

This matches the **High (10,001–15,000 points)** impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* The attacker cost is a single P2P connection and a trivial 9-byte message repeated at wire speed. [13](#0-12) 

## Likelihood Explanation

- Requires only a valid P2P connection — no keys, no PoW, no stake.
- `GetBlockFilterHashes` is a struct with a single `Uint64` field, trivial to construct and send at wire speed.
- The filter protocol is enabled by default on nodes that serve light clients.
- No existing guard (ban, disconnect, backpressure) is triggered by high-frequency valid requests — malformed messages are banned, but well-formed flood messages are not.
- The attack is repeatable and scalable: multiple attacker peers compound the DB read rate linearly. [14](#0-13) 

## Recommendation

Add a `governor`-based `RateLimiter<(PeerIndex, u32)>` to `BlockFilter`, mirroring the pattern already used in `Relayer`:

```rust
// In BlockFilter struct:
rate_limiter: RateLimiter<(PeerIndex, u32)>,

// In BlockFilter::try_process, before the match:
if self.rate_limiter.check_key(&(peer, message.item_id())).is_err() {
    return StatusCode::TooManyRequests.with_context(message.item_name());
}
```

A quota of 10–30 requests/second per peer per message type is consistent with the existing `Relayer` configuration (currently set to 30 req/s): [15](#0-14) 

Also call `self.rate_limiter.retain_recent()` in the `disconnected` handler, as done in `Relayer`: [16](#0-15) 

## Proof of Concept

```python
# Pseudocode — connect to a CKB node's filter protocol port
import socket, struct, time

def make_get_block_filter_hashes(start_number):
    # GetBlockFilterHashes is a struct { start_number: Uint64 }
    # Wrapped in BlockFilterMessage union (item id = 2)
    payload = struct.pack('<Q', start_number)
    # ... molecule encoding omitted for brevity
    return encode_block_filter_message(item_id=2, body=payload)

conn = connect_to_filter_protocol("127.0.0.1", 8115)
t0 = time.time()
count = 0
while time.time() - t0 < 10:
    conn.send(make_get_block_filter_hashes(0))  # triggers up to 4001 DB reads
    count += 1

print(f"Sent {count} messages in 10s → ~{count * 4001} DB reads forced")
# Assert: DB read rate on victim node is proportional to message send rate
# Verify: monitor RocksDB read metrics (e.g., via Prometheus/metrics endpoint)
#         and confirm block validation / relay latency degrades proportionally
```

To verify: monitor RocksDB read metrics on the victim node while sending the flood. The read rate should scale linearly with message rate, and block validation / relay processing latency should degrade measurably. A single attacker peer with a 1 Gbps link can sustain thousands of messages per second, each triggering the full 4,001-read batch. [4](#0-3)

### Citations

**File:** sync/src/filter/mod.rs (L21-25)
```rust
#[derive(Clone)]
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

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L32-79)
```rust
    pub async fn execute(self) -> Status {
        let active_chain = self.filter.shared.active_chain();
        let start_number: BlockNumber = self.message.to_entity().start_number().into();
        let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();

        let mut block_filter_hashes = Vec::new();

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

            let mut block_number = start_number;
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
            let content = packed::BlockFilterHashes::new_builder()
                .start_number(start_number)
                .parent_block_filter_hash(parent_block_filter_hash)
                .block_filter_hashes(block_filter_hashes)
                .build();

            let message = packed::BlockFilterMessage::new_builder()
                .set(content)
                .build();
            async_send_message_to(&self.nc, self.peer, &message).await
        } else {
            Status::ignored()
        }
```

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-10)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;
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

**File:** sync/src/relayer/mod.rs (L933-935)
```rust
        // Retains all keys in the rate limiter that were used recently enough.
        self.rate_limiter.retain_recent();
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
