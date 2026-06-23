### Title
Unbounded Memory Growth in `HolePunching` `forward_rate_limiter` via Attacker-Controlled PeerIds - (File: `network/src/protocols/hole_punching/mod.rs`)

---

### Summary

The `HolePunching` protocol maintains a `forward_rate_limiter` keyed by `(PeerId, PeerId, u32)` tuples extracted from the **message body** (not the session). Any connected peer can send `ConnectionRequest`, `ConnectionRequestDelivered`, or `ConnectionSync` messages with arbitrary attacker-controlled `from` and `to` PeerIds, inserting a new entry into the limiter's `HashMapStateStore` for each unique pair. The limiter's internal state is never pruned during an active connection — `retain_recent()` is only called on `disconnected`. This allows a single connected peer to grow the limiter's backing `HashMap` without bound, exhausting node memory.

---

### Finding Description

`HolePunching` holds two rate limiters:

```rust
rate_limiter: RateLimiter<(PeerIndex, u32)>,          // keyed by session
forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>, // keyed by message-body fields
``` [1](#0-0) 

The outer `rate_limiter` is keyed by `(session_id, item_id)` — bounded by the number of connected peers. The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)` where `from` and `to` are deserialized from the message payload: [2](#0-1) 

The `from` and `to` fields are fully attacker-controlled. Each unique `(from, to, item_id)` triple causes `governor::RateLimiter::check_key()` to insert a new entry into the `HashMapStateStore`. The same pattern exists in `ConnectionRequestDeliveredProcess` and `ConnectionSyncProcess`: [3](#0-2) [4](#0-3) 

Cleanup of the `forward_rate_limiter` only occurs on peer disconnect: [5](#0-4) 

The `notify()` handler (fires every 5 minutes) prunes `inflight_requests` and `pending_delivered` by timestamp, but **never calls `retain_recent()` on `forward_rate_limiter`**: [6](#0-5) 

The outer `rate_limiter` allows 30 messages per second per `(session_id, item_id)`: [7](#0-6) 

With 3 message types and 30 msg/sec each, an attacker can insert up to **90 new entries/second** into `forward_rate_limiter` while staying within the outer rate limit. Over a 24-hour connection: 90 × 86,400 = **7,776,000 entries**. Each entry holds two `PeerId` values (~39 bytes each) plus a `u32` plus HashMap overhead — roughly 150–200 bytes per entry — yielding **~1.2–1.5 GB** of memory growth from a single peer connection.

---

### Impact Explanation

An attacker who establishes a single P2P connection to a CKB node and continuously sends `ConnectionRequest` (or `ConnectionRequestDelivered` / `ConnectionSync`) messages with unique `from`/`to` PeerIds will cause the node's `forward_rate_limiter` to grow without bound. Over hours, this exhausts available memory, triggering OOM conditions that crash or severely degrade the node. A crashed node cannot process blocks, relay transactions, or serve RPC clients — a complete service outage for all users of that node.

---

### Likelihood Explanation

Any unprivileged peer reachable over the CKB P2P network can trigger this. No tokens, keys, or special privileges are required — only a TCP connection to a node that has the `HolePunching` protocol enabled. The attack is sustained at 30 msg/sec per message type, well within normal network capacity. The attacker can maintain the connection indefinitely, and the node has no periodic cleanup mechanism to bound the limiter's memory.

---

### Recommendation

1. **Add periodic `retain_recent()` calls** inside the `notify()` handler (which already fires every 5 minutes) for both `rate_limiter` and `forward_rate_limiter`.
2. **Cap the `forward_rate_limiter` entry count** — use a bounded LRU-backed state store instead of an unbounded `HashMapStateStore`, or reject messages whose `(from, to)` pair is not a currently-connected peer.
3. **Validate `from` against the sending session** — if `content.from` does not match the actual session's peer ID, reject the message before it reaches the rate limiter.

---

### Proof of Concept

1. Attacker establishes a P2P connection to a target CKB node with `HolePunching` enabled.
2. Attacker sends `ConnectionRequest` messages at 30/sec, each with a freshly generated random `from` PeerId and `to` PeerId (both are arbitrary byte strings that parse as valid `PeerId`).
3. Each message passes the outer `rate_limiter` check (keyed by session, not by `from`/`to`) and reaches `forward_rate_limiter.check_key(&(from, to, item_id))`, inserting a new entry.
4. After 24 hours: ~2.6M entries from `ConnectionRequest` alone (plus additional entries from the other two message types), consuming ~500MB–1.5GB of heap memory.
5. Node OOM-kills or becomes unresponsive; all users of that node lose access to transaction submission, block relay, and RPC.

The root cause is confirmed at: [8](#0-7) [5](#0-4) [2](#0-1)

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L31-47)
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
}
```

**File:** network/src/protocols/hole_punching/mod.rs (L66-70)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
        debug!("HolePunching.disconnected session={}", context.session.id);
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

**File:** network/src/protocols/hole_punching/mod.rs (L249-257)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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
