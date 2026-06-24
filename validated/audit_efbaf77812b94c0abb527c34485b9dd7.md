Audit Report

## Title
Unbounded `forward_rate_limiter` Heap Growth via Persistent Peer Sending Unique `(from, to)` Pairs — (`network/src/protocols/hole_punching/mod.rs`)

## Summary

The `HolePunching` protocol's `forward_rate_limiter` is a `governor::RateLimiter` backed by a `HashMapStateStore<(PeerId, PeerId, u32)>`. Its only eviction call, `retain_recent()`, is placed exclusively in `disconnected()`. A persistent peer that never disconnects can insert an unbounded number of unique `(from, to)` key pairs by crafting messages with attacker-controlled `from`/`to` fields, causing unbounded heap growth that can exhaust node memory and crash the node.

## Finding Description

**Rate limiter type and key space:**

`forward_rate_limiter` is declared as `RateLimiter<(PeerId, PeerId, u32)>` backed by `HashMapStateStore`, meaning every unique key seen creates a new heap-allocated entry. [1](#0-0) 

**Eviction only on disconnect:**

`retain_recent()` is called on both rate limiters only inside `disconnected()`: [2](#0-1) 

The `notify()` callback (fired every `CHECK_INTERVAL = 5 minutes`) evicts `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on either rate limiter: [3](#0-2) 

**Attacker-controlled key space:**

The `forward_rate_limiter` key is `(content.from, content.to, msg_item_id)` where `from` and `to` are parsed directly from message bytes with no requirement that `from` matches the actual session peer ID: [4](#0-3) 

The `from`/`to` fields are parsed with only structural validity checks (valid PeerId encoding): [5](#0-4) 

**Outer rate limiter does not bound key-space growth:**

The outer `rate_limiter` is keyed by `(session_id, msg.item_id())` and limits to 30 messages/second per session per message type. This caps the **insertion rate** at 30 entries/second but places **no cap on the total number of unique `(from, to)` pairs** that accumulate in the `HashMapStateStore`: [6](#0-5) 

## Impact Explanation

Each `(PeerId, PeerId, u32)` key is approximately 82 bytes (two 38-byte multihash PeerIds + 4-byte u32) plus `HashMap` overhead (~100–150 bytes/entry). At 30 insertions/second, the map grows by ~6 KB/second, or roughly 500 MB/day from a single persistent connection. No periodic eviction exists while the connection is alive. The result is unbounded heap growth leading to OOM or severe memory pressure, crashing the victim node.

**Impact: High — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

Requires only a single unprivileged P2P connection with no PoW, keys, or special privileges. The attacker keeps the TCP connection alive and streams `ConnectionRequest` messages with randomly generated `from`/`to` PeerIds at the allowed rate (30/second). The `HolePunching` protocol is enabled by default when `SupportProtocol::HolePunching` is in the config. The attack is fully repeatable and requires no victim mistakes.

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
// forward_rate_limiter internal HashMapStateStore has N entries, never reclaimed
// Memory grows monotonically to N * ~200 bytes
```

Each iteration inserts a fresh `(from, to, item_id)` key. Since `disconnected()` is never called, `retain_recent()` is never invoked, and the map size grows monotonically to N with no upper bound. [7](#0-6)

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

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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
