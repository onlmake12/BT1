### Title
Unbounded `forward_rate_limiter` HashMap Growth via Persistent Connection with Unique `(from, to)` PeerIds — (`network/src/protocols/hole_punching/mod.rs`)

### Summary

The `forward_rate_limiter` in the `HolePunching` protocol uses a `HashMapStateStore` keyed by attacker-controlled `(PeerId, PeerId, u32)` tuples. The only cleanup call, `retain_recent()`, is invoked exclusively on peer disconnect. A persistent connection that never disconnects can grow this HashMap without bound by sending messages with unique `from`/`to` PeerId pairs, limited only by the outer per-session rate limiter (30 req/sec).

### Finding Description

`forward_rate_limiter` is declared as:

```rust
forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>
```

backed by `governor::state::keyed::HashMapStateStore`. [1](#0-0) 

The key inserted on every forwarded message is `(content.from, content.to, msg_item_id)`, where `from` and `to` are deserialized directly from the wire message — fully attacker-controlled PeerIds: [2](#0-1) 

The same pattern applies to `ConnectionRequest` and `ConnectionRequestDelivered`: [3](#0-2) 

`retain_recent()` is called **only** in `disconnected()`: [4](#0-3) 

The periodic `notify()` timer (every 5 minutes) cleans up `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on either rate limiter: [5](#0-4) 

The outer per-session rate limiter (`rate_limiter`, keyed by `(session_id, msg_item_id)`) allows 30 req/sec per session: [6](#0-5) 

This outer limiter does not bound the number of distinct keys inserted into `forward_rate_limiter` — it only limits the rate at which new unique `(from, to)` pairs are added.

### Impact Explanation

Each unique `(from, to)` pair inserts a new `HashMap` entry. `PeerId` is a multihash (~39 bytes), so each key is ~82 bytes plus governor's `AtomicU64` state plus `HashMap` overhead (~100–150 bytes/entry total). At 30 entries/sec over 24 hours: ~2.6M entries ≈ 300–400 MB from a single persistent connection. Multiple concurrent attackers multiply this linearly. This leads to OOM on memory-constrained nodes.

### Likelihood Explanation

Any unprivileged peer can open a connection to a CKB node that has the HolePunching protocol enabled. No PoW, no key, no special role is required. The attacker simply maintains the TCP session and sends valid `ConnectionSync` (or `ConnectionRequest`/`ConnectionRequestDelivered`) messages with freshly generated random `from`/`to` PeerIds at the allowed rate.

### Recommendation

Call `self.forward_rate_limiter.retain_recent()` inside the `notify()` handler (which already fires every 5 minutes) in addition to `disconnected()`. This bounds the HashMap to entries active within the last rate-limiter window regardless of connection lifetime.

### Proof of Concept

1. Connect to a CKB node with HolePunching enabled.
2. In a loop, send `ConnectionSync{from=random_peer_id_i, to=random_peer_id_j, route=[]}` at ≤30/sec.
3. Never disconnect.
4. After N seconds, `forward_rate_limiter`'s internal HashMap contains N×30 entries.
5. Assert: `HashMap` size grows monotonically; no eviction occurs until disconnect.

The `notify()` at `CHECK_INTERVAL` (5 min) does not help — it only prunes `pending_delivered` and `inflight_requests`. [5](#0-4)

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

**File:** network/src/protocols/hole_punching/mod.rs (L169-175)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let status = self.network_state.connection_status();

        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L85-96)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionSync",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionSync");
        }
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
