Audit Report

## Title
Unbounded `forward_rate_limiter` Heap Growth via Persistent Peer Sending Unique `(from, to)` Pairs — (`network/src/protocols/hole_punching/mod.rs`)

## Summary
The `HolePunching` protocol's `forward_rate_limiter` uses a `governor::RateLimiter` backed by a `HashMapStateStore<(PeerId, PeerId, u32)>`. Its only eviction call, `retain_recent()`, is placed exclusively in `disconnected()`. A persistent peer that never disconnects can insert an unbounded number of unique `(from, to, item_id)` key triples into the store by crafting messages with attacker-controlled `from`/`to` fields, causing unbounded heap growth that can OOM-crash the victim node.

## Finding Description

**Eviction only on disconnect:**

`retain_recent()` is called only inside `disconnected()`: [1](#0-0) 

The `notify()` callback (fired every `CHECK_INTERVAL = 5 minutes`) evicts `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on either rate limiter: [2](#0-1) 

**Attacker-controlled key space:**

The `forward_rate_limiter` key is `(PeerId, PeerId, u32)` where `from` and `to` come directly from message content, not from the session identity: [3](#0-2) 

For `ConnectionRequest`, `from` and `to` are parsed from raw bytes with only structural validity checks — no requirement that `from` matches the actual session peer ID: [4](#0-3) 

The `forward_rate_limiter` is then keyed with these attacker-supplied values: [5](#0-4) 

The same pattern applies to `ConnectionRequestDelivered`: [6](#0-5) 

**Outer rate limiter does not bound key-space growth:**

The outer `rate_limiter` is keyed by `(session_id, msg.item_id())` and limits throughput to 30 messages/second per session per message type: [7](#0-6) 

This caps the **insertion rate** at 30 entries/second per message type (90/second across all 3 types), but places **no cap on the total number of unique `(from, to)` pairs** that can be inserted. Each message with a fresh `(from, to)` pair adds a new entry to the `HashMapStateStore` that is never reclaimed while the connection remains alive.

## Impact Explanation

A single unprivileged persistent P2P connection can grow the `forward_rate_limiter`'s internal `HashMap` without bound. Each `(PeerId, PeerId, u32)` key is approximately 82 bytes plus HashMap overhead (~100–150 bytes/entry). At 30 insertions/second per message type across 3 types, the map grows at ~18 KB/second, or roughly 1.5 GB/day from a single connection. Multiple coordinated attackers multiply this linearly. The result is unbounded heap growth leading to OOM or severe memory pressure, crashing the victim CKB node.

This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

- Requires only a single unprivileged P2P connection — no PoW, no keys, no special privileges.
- The attacker keeps the TCP connection alive and streams crafted messages at the allowed rate (30/sec per type).
- `from` and `to` PeerIds can be randomly generated per message; no coordination with real peers is needed, only structural validity (valid multihash encoding).
- The HolePunching protocol is enabled by default when `SupportProtocol::HolePunching` is in the config. [8](#0-7) 

## Recommendation

Call `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify()` callback so stale entries are periodically evicted regardless of whether peers disconnect:

```rust
async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
    // Evict stale rate-limiter entries periodically
    self.rate_limiter.retain_recent();
    self.forward_rate_limiter.retain_recent();
    // ... rest of notify
}
```

Additionally, consider validating that `from` matches the actual session peer ID before inserting into `forward_rate_limiter`, or switching to a capacity-limited structure (e.g., an LRU-backed keyed store) to hard-cap memory usage.

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

Each iteration inserts a fresh `(from, to, 0)` key. Since `disconnected()` is never called, `retain_recent()` is never invoked, and the map size grows monotonically to N. The outer rate limiter allows up to 30 such insertions per second per message type, so N can reach millions over hours from a single connection.

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L46-46)
```rust
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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L134-145)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionRequestDelivered",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionRequestDelivered");
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
