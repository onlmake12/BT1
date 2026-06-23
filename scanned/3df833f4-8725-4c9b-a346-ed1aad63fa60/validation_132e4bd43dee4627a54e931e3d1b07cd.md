### Title
Unbounded `forward_rate_limiter` HashMapStateStore Growth via Attacker-Controlled PeerId Keys — (`network/src/protocols/hole_punching/mod.rs`)

---

### Summary

The `HolePunching` protocol's `forward_rate_limiter` uses a `HashMapStateStore` keyed by `(PeerId, PeerId, u32)` where both `PeerId` values come from the **message body** (attacker-controlled), not from the session identity. Because `retain_recent()` is called **only** in `disconnected()` and never in the periodic `notify()` timer, a persistent connection can accumulate entries in the map indefinitely at the rate permitted by the outer session rate limiter (30/sec), causing unbounded memory growth.

---

### Finding Description

`forward_rate_limiter` is declared as: [1](#0-0) 

The key inserted on every forwarded message is: [2](#0-1) 

`content.from` and `content.to` are deserialized directly from the wire message body: [3](#0-2) 

They are **not** validated against the actual session peer ID, so an attacker can supply arbitrary byte strings as `from`/`to`, generating an unbounded number of distinct map keys.

`retain_recent()` is called only on peer disconnect: [4](#0-3) 

The periodic `notify()` handler (fires every 5 minutes) cleans up `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on either rate limiter: [5](#0-4) 

So during any persistent connection, expired entries accumulate in the `HashMapStateStore` without ever being evicted.

---

### Impact Explanation

The outer `rate_limiter` (keyed by `(PeerIndex, u32)`) caps a single session to 30 `ConnectionRequest`-type messages per second. Each message with a fresh `(from, to)` pair inserts one new entry into `forward_rate_limiter`. With a persistent connection:

- Growth rate: **30 entries/sec per session**
- After 1 hour: ~108,000 entries per session
- With N concurrent sessions (up to `max_outbound`): N × 30 entries/sec

Each entry holds two `PeerId` values (~39 bytes each) plus governor internal state (~tens of bytes), so memory consumption grows proportionally to connection duration. Over hours this reaches tens to hundreds of MB per session, matching the **Medium (2001–10000)** memory-exhaustion scope.

---

### Likelihood Explanation

The attack requires only a standard P2P connection to a node with `HolePunching` enabled. No privilege, PoW, or key material is needed. The attacker simply sends `ConnectionRequest` messages with freshly generated `from`/`to` PeerId bytes at the maximum permitted rate. The node cannot distinguish these from legitimate relay traffic.

---

### Recommendation

Add `retain_recent()` calls inside the `notify()` handler so expired entries are evicted periodically regardless of whether peers disconnect:

```rust
// in notify()
self.rate_limiter.retain_recent();
self.forward_rate_limiter.retain_recent();
```

Additionally, consider bounding the `forward_rate_limiter` key on the **session** peer ID (i.e., the actual `PeerIndex` of the relaying peer) rather than the attacker-supplied `from`/`to` fields, which would cap the key space to `max_peers × 3`.

---

### Proof of Concept

```rust
// Pseudocode: single persistent session, unique (from, to) per message
for i in 0..N {
    let from = PeerId::random();
    let to   = PeerId::random();
    send_connection_request(session, from, to);  // passes outer rate limiter (30/sec)
    // forward_rate_limiter now has i+1 entries; retain_recent() never called
}
// assert forward_rate_limiter internal map length ≈ N
// (entries have 1-sec TTL but are never evicted without retain_recent())
```

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

**File:** network/src/protocols/hole_punching/mod.rs (L169-175)
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
