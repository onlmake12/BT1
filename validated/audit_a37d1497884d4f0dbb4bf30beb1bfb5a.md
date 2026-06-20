The code evidence is conclusive. Let me confirm the key comparison points.

### Title
Missing Per-Peer Rate Limiting on `GetBlockFilters` Allows Sustained CPU/I/O DoS — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filters_process.rs`)

---

### Summary

The `BlockFilter` protocol handler processes `GetBlockFilters` messages with no per-peer rate limiting. Each request causes up to 1,000 RocksDB reads and serialization of up to 1.8 MB of filter data. A single remote peer can flood the node with these requests, causing sustained CPU and I/O exhaustion. The same codebase applies rate limiting to the `Relayer` and `HolePunching` protocols, confirming this is an unintentional omission.

---

### Finding Description

`BlockFilter` is the production Filter protocol handler. Its struct definition contains only a `shared` field — no rate limiter: [1](#0-0) 

Its `try_process` method dispatches directly to `GetBlockFiltersProcess::execute()` with no rate check: [2](#0-1) 

Inside `execute()`, the handler iterates up to `BATCH_SIZE = 1000` blocks, performs two RocksDB lookups per block (`get_block_hash` + `get_block_filter`), accumulates data up to the 1.8 MB cap, and serializes a full response — all unconditionally per request: [3](#0-2) [4](#0-3) 

By contrast, the `Relayer` protocol carries an explicit `rate_limiter: RateLimiter<(PeerIndex, u32)>` field and enforces it before any dispatch: [5](#0-4) [6](#0-5) 

The `HolePunching` protocol similarly has both `rate_limiter` and `forward_rate_limiter` and checks them in `received`: [7](#0-6) [8](#0-7) 

The `BlockFilter` protocol is the only request-serving protocol that omits this guard entirely.

---

### Impact Explanation

Each `GetBlockFilters{start_number: 0}` message causes:
- Up to 1,000 RocksDB key lookups (block hash + filter data per block)
- Accumulation and serialization of up to ~1.8 MB of response data
- A large outbound write on the network socket

A single attacker peer sending these in a tight loop can saturate the node's I/O scheduler, RocksDB read threads, and outbound bandwidth. Because the `BlockFilter` handler is `async` and processes messages sequentially per peer session, a flood from even one peer creates a sustained, measurable resource drain. Multiple concurrent attacker connections multiply the effect linearly.

---

### Likelihood Explanation

The attack requires only a standard P2P connection to the Filter protocol — no authentication, no PoW, no stake. The message is a 8-byte struct (`start_number: Uint64`). The attacker controls `start_number` freely; setting it to `0` always maximizes work on a synced node. The exploit is trivially scriptable and requires no special knowledge beyond the public CKB P2P protocol spec.

---

### Recommendation

Add a `governor`-based `RateLimiter<(PeerIndex, u32)>` to `BlockFilter`, mirroring the pattern already used in `Relayer`:

1. Add `rate_limiter: RateLimiter<(PeerIndex, u32)>` to the `BlockFilter` struct.
2. In `BlockFilter::try_process`, check `self.rate_limiter.check_key(&(peer, message.item_id()))` before dispatching, returning `StatusCode::TooManyRequests` on failure.
3. Call `self.rate_limiter.retain_recent()` in `disconnected`.
4. Apply the same fix to `GetBlockFilterHashes` and `GetBlockFilterCheckPoints` handlers, which have the same omission. [9](#0-8) 

---

### Proof of Concept

```
1. Connect to victim node advertising SupportProtocols::Filter.
2. In a tight loop, send:
     GetBlockFilters { start_number: 0 }
   encoded as a BlockFilterMessage molecule struct.
3. Observe on the victim:
   - RocksDB read IOPS spike (up to 2000 reads/request × request rate)
   - CPU usage increase from molecule serialization of ~1.8 MB responses
   - Outbound bandwidth saturation (~1.8 MB × request rate)
4. Repeat from N concurrent connections to multiply impact.
```

The `GetBlockFilters` struct is only 8 bytes, so the attacker's inbound bandwidth cost is negligible compared to the victim's outbound and I/O cost — a classic amplification asymmetry.

### Citations

**File:** sync/src/filter/mod.rs (L21-25)
```rust
#[derive(Clone)]
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L33-44)
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
```

**File:** sync/src/filter/mod.rs (L119-160)
```rust
impl CKBProtocolHandler for BlockFilter {
    async fn init(&mut self, _nc: Arc<dyn CKBProtocolContext + Sync>) {}

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
            crate::LOG_TARGET_FILTER,
            "process message={}, peer={}, cost={:?}",
            msg.item_name(),
            peer_index,
            Instant::now().saturating_duration_since(start_time),
        );
    }
```

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** sync/src/filter/get_block_filters_process.rs (L45-72)
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
                let Some(next_block_number) = block_number.checked_add(1) else {
                    break;
                };
                block_number = next_block_number;
            }
```

**File:** sync/src/relayer/mod.rs (L78-82)
```rust
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
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

**File:** network/src/protocols/hole_punching/mod.rs (L44-47)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
}
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
