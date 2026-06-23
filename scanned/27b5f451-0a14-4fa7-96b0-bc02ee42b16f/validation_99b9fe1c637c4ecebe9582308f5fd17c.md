### Title
Unbounded `forward_rate_limiter` HashMapStateStore Growth via Attacker-Controlled `from` PeerId in ConnectionRequest — (`network/src/protocols/hole_punching/mod.rs`, `network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The `HolePunching` protocol's `forward_rate_limiter` uses a `HashMapStateStore` keyed by `(PeerId, PeerId, u32)` derived from message-payload fields. Because the `from` field is attacker-controlled and never validated against the actual sending peer's identity, a single connected peer can insert an unbounded number of distinct keys into the map. `retain_recent()` is called only on peer disconnect, so the map grows without bound for the lifetime of any persistent connection, leading to memory exhaustion and OOM crash of the relay node.

---

### Finding Description

`forward_rate_limiter` is declared as:

```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;
// ...
forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
``` [1](#0-0) 

The key inserted on every `ConnectionRequest` is:

```rust
.check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
``` [2](#0-1) 

`content.from` is parsed directly from the message bytes with only syntactic validity checked (valid multihash bytes), with no check that it matches the actual session peer:

```rust
let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
    StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
})?;
``` [3](#0-2) 

`retain_recent()` is called **only** in `disconnected()`:

```rust
async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
    self.rate_limiter.retain_recent();
    self.forward_rate_limiter.retain_recent();
``` [4](#0-3) 

The periodic `notify()` callback (fired every 5 minutes) cleans `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on either rate limiter: [5](#0-4) 

The outer per-session `rate_limiter` (keyed by `(session_id, msg_item_id)`) limits throughput to 30 messages/second per session/type pair, but this does not bound the number of distinct `from` values that can be injected — it only throttles the insertion rate: [6](#0-5) 

---

### Impact Explanation

A single persistent connection can insert 30 new `HashMapStateStore` entries per second. Each entry holds two `PeerId` values (~39 bytes each), a `u32`, and governor's internal rate-limiter state. At 30 entries/second over 24 hours, this yields ~2.6 million entries (~500 MB+) from one attacker connection. Multiple simultaneous connections multiply the rate linearly. The `HashMapStateStore` has no capacity cap. The result is unbounded heap growth, eventual OOM, and process crash — partitioning the relay node from the network.

---

### Likelihood Explanation

The attack requires only a single valid P2P connection to a relay node, which is an open, unprivileged operation on the CKB mainnet. The attacker needs no special keys, no PoW, and no privileged role. The `from` field is freely settable to any syntactically valid `PeerId` bytes. The attack is sustainable indefinitely as long as the connection is held open.

---

### Recommendation

1. **Periodic cleanup**: Call `self.forward_rate_limiter.retain_recent()` inside `notify()` (every 5 minutes) in addition to `disconnected()`.
2. **Bind `from` to session identity**: Validate that `content.from` matches the actual `PeerId` of the sending session before inserting into `forward_rate_limiter`. This collapses the key space to `(actual_peer_id, to, msg_item_id)`, which is bounded by the number of connected peers.
3. **Cap map size**: Enforce a maximum entry count on `forward_rate_limiter` and reject or evict entries beyond the cap.

---

### Proof of Concept

```rust
// Pseudocode unit test
let relay = HolePunching::new(network_state);
let session = connect_peer(&relay); // single connection

for i in 0..N {
    let fake_from = PeerId::random(); // distinct each iteration
    let msg = build_connection_request(fake_from, some_to, valid_addrs);
    relay.received(session, msg).await;
}

// Without disconnect, retain_recent() is never called.
// Assert: forward_rate_limiter internal map size == N
assert_eq!(relay.forward_rate_limiter.len(), N);
```

The outer `rate_limiter` throttles to 30 inserts/second but does not prevent the map from reaching size N given sufficient time. At 30/s, N = 2,592,000 entries is reachable in 24 hours from a single connection.

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

**File:** network/src/protocols/hole_punching/mod.rs (L66-69)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
        debug!("HolePunching.disconnected session={}", context.session.id);
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L132-135)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
```
