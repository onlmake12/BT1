Audit Report

## Title
Unbounded `forward_rate_limiter` Heap Growth via Persistent Peer Sending Unique `(from, to)` Pairs — (`network/src/protocols/hole_punching/mod.rs`)

## Summary
The `HolePunching` protocol's `forward_rate_limiter` is a `governor::RateLimiter` backed by a `HashMapStateStore<(PeerId, PeerId, u32)>`. Its only eviction call, `retain_recent()`, is placed exclusively in `disconnected()`. A persistent peer that never disconnects can insert an unbounded number of unique `(from, to)` key pairs by crafting messages with attacker-controlled `from`/`to` fields, causing unbounded heap growth that can exhaust node memory.

## Finding Description
The `forward_rate_limiter` field is declared as `RateLimiter<(PeerId, PeerId, u32)>` backed by a `HashMapStateStore`. [1](#0-0) 

`retain_recent()` is called only inside `disconnected()`, meaning no eviction occurs while a connection is alive: [2](#0-1) 

The `notify()` callback (fired every `CHECK_INTERVAL = 5 minutes`) evicts `pending_delivered` and `inflight_requests` but never calls `retain_recent()` on either rate limiter: [3](#0-2) 

The `forward_rate_limiter` key is `(content.from, content.to, msg_item_id)`, where `from` and `to` are parsed directly from message bytes with no requirement that `from` matches the actual session peer ID: [4](#0-3) 

The `from`/`to` fields are parsed from raw bytes with only structural validity checks (valid PeerId encoding): [5](#0-4) 

The outer `rate_limiter` is keyed by `(session_id, msg.item_id())` and limits to 30 messages/second per session per message type. This caps the **insertion rate** but places no cap on the total number of unique `(from, to)` pairs that can be inserted: [6](#0-5) 

## Impact Explanation
This maps to **High: Vulnerabilities which could easily crash a CKB node**. At 30 insertions/second per message type (90/second across all 3 types), the `HashMapStateStore` grows by approximately 90 × ~200 bytes ≈ 18 KB/second, or ~1.5 GB/day from a single persistent connection. Since `retain_recent()` is never called while the connection is alive, the map grows monotonically until the node runs out of memory and crashes. Multiple coordinated attackers multiply this linearly.

## Likelihood Explanation
Requires only a single unprivileged P2P connection — no proof-of-work, no keys, no special privileges. The attacker keeps the TCP connection alive and streams crafted `ConnectionRequest` messages at the allowed rate (30/second), using randomly generated `from`/`to` PeerIds per message. No coordination with real peers is needed. The HolePunching protocol is enabled by default when `SupportProtocol::HolePunching` is in the config. [7](#0-6) 

## Recommendation
Call `self.rate_limiter.retain_recent()` and `self.forward_rate_limiter.retain_recent()` inside the `notify()` callback so stale entries are periodically evicted regardless of whether peers disconnect:

```rust
async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
    self.rate_limiter.retain_recent();
    self.forward_rate_limiter.retain_recent();
    // ... rest of notify
}
```

Additionally, consider validating that `from` matches the actual session peer ID before inserting into `forward_rate_limiter`, or switching to a capacity-limited structure (e.g., an LRU-backed keyed store) to bound worst-case memory usage.

## Proof of Concept
```rust
// Pseudocode unit test
let mut hp = HolePunching::new(network_state);
for i in 0..N {
    let from = PeerId::random();
    let to   = PeerId::random();
    let msg  = build_connection_request(from, to, /* valid listen_addrs */);
    hp.received(mock_context(session_id), msg).await;
    // Never call hp.disconnected()
}
// Assert: forward_rate_limiter internal map has N entries and was never reclaimed
assert_eq!(hp.forward_rate_limiter.len(), N);
```

Each iteration inserts a fresh `(from, to, 0)` key. Since `disconnected()` is never called, `retain_recent()` is never invoked, and the map size grows monotonically to N. The outer rate limiter allows up to 30 such insertions per second per message type, confirming the insertion rate is bounded only by the per-session throughput cap, not by total map size.

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L31-46)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

/// Hole Punching Protocol
pub(crate) struct HolePunching {
    network_state: Arc<NetworkState>,
    bind_addr: Option<SocketAddr>,
    // Request timestamp recorded
    inflight_requests: HashMap<PeerId, u64>,
    // Delivered timestamp recorded
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L66-70)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
        debug!("HolePunching.disconnected session={}", context.session.id);
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

**File:** network/src/protocols/hole_punching/mod.rs (L169-176)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let status = self.network_state.connection_status();

        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);

```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-40)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L132-143)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionRequest",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionRequest");
        }
```

**File:** network/src/network.rs (L940-953)
```rust
        // HolePunching protocol
        #[cfg(not(target_family = "wasm"))]
        if config
            .support_protocols
            .contains(&SupportProtocol::HolePunching)
        {
            let hole_punching_state = Arc::clone(&network_state);
            let hole_punching_meta =
                SupportProtocols::HolePunching.build_meta_with_service_handle(move || {
                    ProtocolHandle::Callback(Box::new(
                        crate::protocols::hole_punching::HolePunching::new(hole_punching_state),
                    ))
                });
            protocol_metas.push(hole_punching_meta);
```
