### Title
`forward_rate_limiter` keyed by attacker-controlled `(from, to)` pair enables O(K) forwarding amplification and unbounded HashMap growth — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

---

### Summary

The `forward_rate_limiter` in the `HolePunching` protocol is keyed by `(content.from, content.to, msg_item_id)`, where `from` and `to` are **fully attacker-controlled** values from the message payload. An attacker with K simultaneous sessions can send K `ConnectionSync` messages each with a unique `(from_i, to)` pair, bypassing the per-pair rate limit entirely and achieving O(K) forwarding throughput. Additionally, the `forward_rate_limiter` HashMap grows unboundedly because `retain_recent()` is never called periodically — only on peer disconnect.

---

### Finding Description

**Two-layer rate limiting design**

The `HolePunching` protocol uses two rate limiters:

1. **Session-level `rate_limiter`**: keyed by `(session_id, msg.item_id())`, 30 req/sec per session. [1](#0-0) 

2. **Forward `forward_rate_limiter`**: keyed by `(content.from, content.to, msg_item_id)`, 1 req/sec per unique `(from, to)` pair. [2](#0-1) 

The `forward_rate_limiter` type is `RateLimiter<(PeerId, PeerId, u32)>` backed by a `HashMapStateStore`: [3](#0-2) 

It is initialized with a quota of 1 req/sec per key: [4](#0-3) 

**The flaw: `from` and `to` are attacker-controlled**

`SyncContent::try_from` parses `from` and `to` directly from the wire message with no validation against the sending session's actual peer ID: [5](#0-4) 

An attacker can freely set `from` to any arbitrary `PeerId` bytes. Each unique `(from_i, to, item_id)` tuple creates a fresh bucket in the `HashMapStateStore`, so the 1/sec limit applies independently per pair — not globally.

**No periodic cleanup of the HashMap**

`retain_recent()` is only called in the `disconnected` handler: [6](#0-5) 

The `notify` handler (fired every 5 minutes) cleans up `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on either rate limiter: [7](#0-6) 

As long as the attacker maintains sessions, the `forward_rate_limiter` HashMap grows without bound.

---

### Impact Explanation

**Forwarding amplification**: With K inbound sessions (up to `max_peers - max_outbound_peers = 125 - 8 = 117` by default), the attacker sends K messages each with a unique `from_i`, each passing both rate limiters independently. The victim performs K `forward_sync` calls, each sending a `ConnectionSync` message to the `to` peer. This is O(K) forwarding work for O(K) attacker messages — no amplification ratio, but the victim's outbound bandwidth and CPU are consumed proportionally to attacker session count. [8](#0-7) 

**Memory exhaustion**: A single session sending 30 unique `(from, to)` pairs/sec adds 30 entries/sec to the HashMap. With 117 sessions: 3,510 entries/sec. Over 5 minutes (one `CHECK_INTERVAL`): ~1,053,000 entries. Each entry holds two `PeerId` values (~32 bytes each) plus governor state overhead. `retain_recent()` is never called while sessions are live, so memory grows monotonically. [9](#0-8) 

---

### Likelihood Explanation

The attack requires only inbound TCP connections to the victim — no PoW, no keys, no privileged access. The default `max_peers = 125` allows up to 117 inbound sessions from a single attacker IP (or distributed across IPs to avoid IP-level bans). The `from` field is never validated against the session's actual peer ID, making the bypass trivial to implement. [10](#0-9) 

---

### Recommendation

1. **Key the `forward_rate_limiter` by `(session_id, msg_item_id)` instead of `(from, to, msg_item_id)`**, or add a global cap on total forwarding throughput per unit time regardless of `(from, to)` diversity.
2. **Call `forward_rate_limiter.retain_recent()` in the `notify` handler** (every 5 minutes) to bound HashMap memory growth, mirroring how `pending_delivered` and `inflight_requests` are pruned. [7](#0-6) 

---

### Proof of Concept

```
Preconditions:
  - Victim V has max_peers=125, max_outbound_peers=8 → accepts up to 117 inbound sessions
  - Attacker controls K=117 TCP connections to V, each with a distinct peer ID P_1..P_K
  - Attacker knows one peer Q connected to V (e.g., one of their own sessions)

Attack loop (per second):
  For i in 1..=K:
    session_i sends: ConnectionSync { from = random_peer_id_i, to = Q, route = [] }

Per message processing on V:
  1. rate_limiter.check_key(&(session_i, SYNC_ITEM_ID)) → OK  (30/sec budget for session_i)
  2. forward_rate_limiter.check_key(&(random_peer_id_i, Q, SYNC_ITEM_ID)) → OK
     (fresh bucket, never seen before)
  3. forward_sync(&Q) → V sends ConnectionSync to Q

Result after 1 second:
  - V has forwarded K=117 messages to Q
  - forward_rate_limiter HashMap has grown by K=117 new entries

Result after T seconds (sessions maintained):
  - V has forwarded K*T messages to Q
  - forward_rate_limiter HashMap has K*30*T entries (30 unique pairs/sec/session)
  - Memory grows at ~3,510 entries/sec with K=117 sessions
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L25-25)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
```

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

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L29-48)
```rust
impl TryFrom<&packed::ConnectionSyncReader<'_>> for SyncContent {
    type Error = Status;

    fn try_from(value: &packed::ConnectionSyncReader<'_>) -> Result<Self, Self::Error> {
        let route = value
            .route()
            .iter()
            .map(|id| {
                PeerId::from_bytes(id.raw_data().to_vec()).map_err(|_| {
                    StatusCode::InvalidRoute.with_context("the route peer id is invalid")
                })
            })
            .collect::<Result<Vec<_>, _>>()?;
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
        Ok(SyncContent { route, from, to })
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L98-104)
```rust
        match content.route.last() {
            Some(next_peer_id) => self.forward_sync(next_peer_id).await,
            None => {
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.to {
                    // forward the message to the `to` peer
                    self.forward_sync(&content.to).await
```

**File:** resource/ckb.toml (L94-95)
```text
max_peers = 125
max_outbound_peers = 8
```
