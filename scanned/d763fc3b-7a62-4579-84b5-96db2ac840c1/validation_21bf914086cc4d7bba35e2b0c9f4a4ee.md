Let me look at the governor crate's `HashMapStateStore` behavior and the `notify` timer to confirm whether `retain_recent` is ever called periodically.

The code confirms the vulnerability. Let me verify the `notify` timer to ensure `retain_recent()` is definitively absent there.

The `notify` function at [1](#0-0)  only cleans up `pending_delivered` and `inflight_requests` — it never calls `retain_recent()` on either rate limiter.

The `forward_rate_limiter` is typed as `RateLimiter<(PeerId, PeerId, u32)>` backed by `HashMapStateStore`: [2](#0-1) 

`retain_recent()` is called **only** in `disconnected()`: [3](#0-2) 

The key inserted into `forward_rate_limiter` is `(content.from, content.to, self.msg_item_id)`, where `from` and `to` are **attacker-controlled** values parsed from the message body: [4](#0-3) 

The outer `rate_limiter` (keyed by `(session_id, msg.item_id())`) limits to 30 messages/second per session, but each of those 30 messages can carry a unique `(from, to)` pair, inserting a new bucket into `forward_rate_limiter` with no eviction until disconnect.

---

### Title
Unbounded `forward_rate_limiter` HashMapStateStore Growth via Attacker-Controlled `(from, to)` Keys — (`network/src/protocols/hole_punching/mod.rs`)

### Summary
The `HolePunching` protocol's `forward_rate_limiter` uses a `HashMapStateStore<(PeerId, PeerId, u32)>` whose entries are never evicted during a live session. An unprivileged peer can insert up to 30 new distinct keys per second by sending `ConnectionSync` (or `ConnectionRequest`/`ConnectionRequestDelivered`) messages with unique attacker-chosen `from`/`to` PeerId pairs. Because `retain_recent()` is only called on `disconnected()`, the map grows without bound for the lifetime of the connection.

### Finding Description
`HolePunching::forward_rate_limiter` is a `governor::RateLimiter` backed by `HashMapStateStore`. The `governor` crate documents that `HashMapStateStore` requires periodic calls to `retain_recent()` to evict stale entries; without it the map grows monotonically.

The only call site for `retain_recent()` is `disconnected()` (mod.rs lines 67–68). The `notify()` timer (fired every 5 minutes via `CHECK_INTERVAL`) cleans `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on either rate limiter.

Every `ConnectionSync`, `ConnectionRequest`, and `ConnectionRequestDelivered` message calls `forward_rate_limiter.check_key(&(content.from, content.to, msg_item_id))`. The `from` and `to` fields are parsed directly from the wire message with no constraint that they correspond to known or connected peers. An attacker can therefore supply an unbounded stream of unique `(from, to)` pairs, each creating a new map entry.

The outer per-session `rate_limiter` (30 req/s per `(session_id, item_id)`) throttles throughput but does not bound the total number of distinct keys inserted over a long-lived connection.

### Impact Explanation
At 30 messages/second, a 24-hour session produces ~2.6 million unique `(PeerId, PeerId, u32)` keys. Each entry in the `HashMapStateStore` holds two `PeerId` values (~39 bytes each) plus a `u32` plus `governor` state and `HashMap` overhead — roughly 200–300 bytes per entry. This yields ~500–750 MB of heap growth from a single attacker session. Multiple concurrent attacker sessions multiply the effect. Memory exhaustion crashes the node process, removing it from consensus participation.

### Likelihood Explanation
The attacker needs only a standard P2P connection (no special privileges, no PoW, no key material). Maintaining a long-lived TCP session and sending 30 crafted messages per second is trivially achievable. The `from`/`to` PeerIds require only valid byte-length encoding (no signature or proof of ownership is checked before the rate-limiter insertion).

### Recommendation
1. Call `self.forward_rate_limiter.retain_recent()` inside the existing `notify()` handler (every 5 minutes) alongside the existing `pending_delivered`/`inflight_requests` cleanup.
2. Alternatively, bound the key space: validate that `from` equals the sending session's peer ID before inserting into `forward_rate_limiter`, eliminating the ability to inject arbitrary keys.
3. Consider switching to a fixed-capacity LRU-based rate-limiter store to provide a hard memory ceiling.

### Proof of Concept
```
1. Connect to a victim CKB node as a normal P2P peer.
2. In a loop, send ConnectionSync messages at 30/s where each message has:
     from = random_peer_id_i   (unique per message)
     to   = random_peer_id_j   (unique per message)
     route = []  (empty, passes the MAX_HOPS check)
3. Maintain the session for several hours.
4. Observe victim node RSS growing proportionally to messages sent.
5. Node OOMs and crashes; no disconnect occurs until the attacker chooses.
```

The root cause is the absence of a periodic `retain_recent()` call on `forward_rate_limiter` in `notify()`, combined with the lack of any constraint on the `from`/`to` key space accepted from the wire.

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

**File:** network/src/protocols/hole_punching/mod.rs (L169-176)
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
