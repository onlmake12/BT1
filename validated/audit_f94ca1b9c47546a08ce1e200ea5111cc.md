### Title
Missing Per-Peer Rate Limiter in `BlockFilter` Protocol Enables Unbounded DB Read Amplification — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filter_hashes_process.rs`)

### Summary

The `BlockFilter` P2P protocol handler has no rate limiter, unlike `Relayer` and `HolePunching` which both carry a `governor`-based `RateLimiter<(PeerIndex, u32)>`. Any unauthenticated peer can flood `GetBlockFilterHashes` messages at wire speed, triggering up to ~4,001 RocksDB reads per message with no throttle, cooldown, or per-peer budget.

### Finding Description

`BlockFilter` is the struct that handles the filter sub-protocol: [1](#0-0) 

It has only one field — `shared` — and no rate-limiter. The `received` entry point dispatches directly to `process` → `try_process` → `GetBlockFilterHashesProcess::execute` with zero admission control: [2](#0-1) 

Inside `execute`, when `latest >= start_number`, the handler performs:
1. One `get_block_hash(start_number - 1)` + `get_block_filter_hash` for the parent (2 DB reads).
2. A loop of up to `BATCH_SIZE = 2000` iterations, each doing `get_block_hash` + `get_block_filter_hash` (up to 4,000 more DB reads). [3](#0-2) 

Only when `start_number > latest` does it short-circuit with `Status::ignored()`: [4](#0-3) 

**Contrast with `Relayer`**, which carries a `rate_limiter` field and gates every non-PoW message before dispatch: [5](#0-4) [6](#0-5) 

**And `HolePunching`**, which has both a per-peer and a per-route rate limiter checked before any processing: [7](#0-6) [8](#0-7) 

`BlockFilter` has neither.

### Impact Explanation

A single attacker peer sending `GetBlockFilterHashes` with `start_number=0` at maximum rate forces the victim node to execute up to **4,001 RocksDB reads per message** with no bound. At even modest message rates (e.g., 1,000 msg/s), this is ~4 million DB reads per second from one peer. Multiple peers compound linearly. This saturates the storage layer, starving block validation, sync, and relay processing — causing network congestion and degraded participation in the CKB network.

The alternating `start_number=latest` / `start_number=latest+1` pattern described in the question is a valid (if suboptimal) variant: `latest` triggers real DB work while `latest+1` returns `ignored()` cheaply, maintaining a high work-to-cost ratio. The more impactful attack is simply `start_number=0` for maximum batch reads.

### Likelihood Explanation

- Requires only a valid P2P connection — no keys, no PoW, no stake.
- The `GetBlockFilterHashes` message is a 9-byte struct (`Uint64` field), trivial to construct.
- The filter protocol is enabled by default on nodes that serve light clients.
- No existing guard (ban, disconnect, backpressure) is triggered by high-frequency valid requests.

### Recommendation

Add a `governor`-based `RateLimiter<(PeerIndex, u32)>` to `BlockFilter`, mirroring the pattern already used in `Relayer`:

```rust
// In BlockFilter::try_process, before the match:
if self.rate_limiter.check_key(&(peer, message.item_id())).is_err() {
    return StatusCode::TooManyRequests.with_context(message.item_name());
}
```

A quota of 10–30 requests/second per peer per message type is consistent with the existing `Relayer` configuration. [9](#0-8) 

### Proof of Concept

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
```

### Citations

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

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L39-66)
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
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L77-79)
```rust
        } else {
            Status::ignored()
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
