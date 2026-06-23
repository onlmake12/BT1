### Title
Unbounded `forward_rate_limiter` Map Growth via Spoofed `from=local_peer_id` in `ConnectionRequestDelivered` — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

---

### Summary

An unprivileged remote peer can cause unbounded memory growth in the `HolePunching` protocol's `forward_rate_limiter` (`HashMapStateStore`) by sending `ConnectionRequestDelivered` messages with `from = local_peer_id`, `route = []`, and a unique `to` peer ID per message. Each unique `(from, to, item_id)` triple inserts a new entry into the backing `HashMap`. The handler returns `StatusCode::Ignore` (no ban). The map is never cleaned up during a live connection because `retain_recent()` is only called on peer disconnect.

---

### Finding Description

**Step 1 — Outer per-session rate limiter (mod.rs:95–107)**

The outer `rate_limiter` is keyed by `(session_id, msg.item_id())`. [1](#0-0) 

`msg.item_id()` is the union discriminant — a fixed constant per message type (e.g., `1` for `ConnectionRequestDelivered`). This limits each session to **30 `ConnectionRequestDelivered` messages per second**, after which further messages are silently dropped. This is the only per-session throttle.

**Step 2 — `forward_rate_limiter.check_key` inserts a new HashMap entry for every unique key (connection_request_delivered.rs:134–145)**

Before any routing logic, `check_key` is called with `(content.from, content.to, msg_item_id)`. [2](#0-1) 

The `forward_rate_limiter` is a `governor::RateLimiter` backed by `HashMapStateStore<(PeerId, PeerId, u32)>`. [3](#0-2) 

`governor`'s `HashMapStateStore::check_key` inserts a new state entry for every previously-unseen key. Since `msg_item_id` is a fixed constant per message type, the effective key space is `(from_peer_id, to_peer_id)`. An attacker who varies `to_peer_id` per message creates a new HashMap entry on every call.

**Step 3 — Spoofed `from = local_peer_id` + empty `route` reaches `StatusCode::Ignore` with no ban (connection_request_delivered.rs:147–176)**

With `route = []`, `content.route.last()` is `None`, so the code enters the terminal branch: [4](#0-3) 

- Line 151: `if self_peer_id != &content.from` — **false** when `from = local_peer_id`, so the forward path is skipped.
- Line 160: `inflight_requests.remove(&content.to)` — returns `None` for any `to` not in the inflight map.
- Line 175: returns `StatusCode::Ignore` — **no ban is issued**.

**Step 4 — `retain_recent()` is never called during a live connection (mod.rs:66–70)**

The only cleanup of `forward_rate_limiter` is: [5](#0-4) 

This is called **only on peer disconnect**, not periodically. Entries accumulate in the `HashMapStateStore` for the entire duration of a connection.

---

### Impact Explanation

- **Growth rate**: 30 new entries/second per session × N max sessions.
- **Entry size**: Each key is `(PeerId, PeerId, u32)` ≈ 39 + 39 + 4 = 82 bytes of key data, plus `HashMap` overhead and `governor` state ≈ 200–300 bytes per entry.
- **Accumulation**: With a long-lived connection and 30 unique `to` peer IDs per second, the map grows at ~9 KB/second per session. With multiple sessions, this scales linearly.
- **No cleanup**: `retain_recent()` is never called during the connection, so stale entries are not evicted.
- **No ban**: `StatusCode::Ignore` does not trigger `ban_session`, so the attacker is never disconnected for this behavior.

The result is unbounded memory growth in the `HolePunching` protocol handler, achievable by any peer that can establish a P2P connection.

---

### Likelihood Explanation

- The attacker only needs a standard P2P connection — no privilege, no key, no PoW.
- The local peer ID is publicly advertised via the identify protocol.
- The attack is rate-limited to 30 messages/second per session by the outer limiter, but this is sufficient for slow, sustained memory exhaustion.
- The path is fully concrete and locally testable.

---

### Recommendation

1. **Call `retain_recent()` periodically** in the `notify` handler (which already fires on `CHECK_INTERVAL`) for both `rate_limiter` and `forward_rate_limiter`, not only on disconnect.
2. **Cap the `forward_rate_limiter` map size**: Reject or evict entries when the map exceeds a configurable bound (e.g., 10,000 entries).
3. **Validate `from` against the sending session's authenticated peer ID**: Reject messages where `content.from` does not match the actual peer ID of the sending session, preventing spoofing of `from = local_peer_id`.

---

### Proof of Concept

```
1. Attacker connects to victim node, learns victim's local_peer_id via identify.
2. Attacker sends up to 30 ConnectionRequestDelivered messages per second:
     from = local_peer_id
     route = []
     to = random_peer_id_i  (unique per message)
     listen_addrs = [any valid addr]
3. Each message:
   - Passes outer rate_limiter (first 30/sec per session)
   - Inserts new entry (local_peer_id, random_peer_id_i, ITEM_ID) into forward_rate_limiter
   - Hits inflight_requests.remove() → None → StatusCode::Ignore (no ban)
4. After M messages, forward_rate_limiter map has M entries.
5. Assert: map size grows monotonically; no ban; no eviction until disconnect.
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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L147-176)
```rust
        match content.route.last() {
            Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
            None => {
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.from {
                    // forward the message to the `from` peer
                    self.forward_delivered(&content.from).await
                } else {
                    // the current peer is the target peer, respond the sync back
                    if let Some(metrics) = ckb_metrics::handle() {
                        metrics.ckb_hole_punching_active_count.inc();
                    }

                    let request_start = self.protocol.inflight_requests.remove(&content.to);

                    match request_start {
                        Some(start) => {
                            let res = self.respond_sync(content.from).await;
                            if !res.is_ok() {
                                return res;
                            }
                            let now = unix_time_as_millis();
                            let ttl = now - start;

                            self.try_nat_traversal(ttl, content.listen_addrs);

                            Status::ok()
                        }
                        None => StatusCode::Ignore.with_context("the request is not in flight"),
                    }
```
