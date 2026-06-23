Now I have all the code I need. Let me analyze the exact mechanics.

### Title
`forward_rate_limiter` Keyed by Attacker-Controlled `(from, to)` Pairs Provides No Global Forwarding Cap — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

---

### Summary

The `forward_rate_limiter` in the HolePunching protocol is keyed by `(from: PeerId, to: PeerId, msg_item_id: u32)` — values that are fully attacker-controlled message fields. An attacker can trivially bypass the limiter by rotating `(from, to)` pairs across messages, achieving unbounded forwarding throughput and unbounded `forward_rate_limiter` HashMap growth from a single session.

---

### Finding Description

**Two rate limiters exist:**

**1. Session rate limiter** (`rate_limiter`): keyed by `(session_id, msg.item_id())`, 30/sec per session. Checked in `received()` before dispatch. [1](#0-0) 

**2. Forward rate limiter** (`forward_rate_limiter`): keyed by `(PeerId, PeerId, u32)` = `(from, to, msg_item_id)`, 1/sec per unique tuple. Checked inside `execute()`. [2](#0-1) 

The `forward_rate_limiter` is declared as `RateLimiter<(PeerId, PeerId, u32)>` backed by a `HashMapStateStore`: [3](#0-2) 

The design intent (per the comment) is: *"the same group of from/to should not be received by the same node more than 1 times within one second."* [4](#0-3) 

**The bypass:** `from` and `to` are parsed directly from the message bytes with no signature verification or binding to the actual sending session: [5](#0-4) 

An attacker sending messages with unique `(from_i, to_i)` pairs gets a fresh rate-limit bucket for each pair. The `forward_rate_limiter` never fires. The only effective global cap is the session rate limiter at 30/sec per session — which the attacker can multiply by opening K sessions.

**Memory exhaustion:** `retain_recent()` is only called on disconnect, not periodically: [6](#0-5) 

During a sustained attack with persistent sessions, the `forward_rate_limiter` HashMap grows at 30×K entries/second and is never pruned. Each entry holds two `PeerId` values (~39 bytes each) plus governor state, so growth is ~100 bytes/entry × 30K entries/sec.

**Forwarding amplification:** `forward_sync()` only sends to a peer if `content.to` is currently connected: [7](#0-6) 

If the attacker uses random fake `to` PeerIds, forwarding fails silently. However, connected peer IDs are discoverable via the discovery protocol, making targeted forwarding amplification realistic.

---

### Impact Explanation

- **Memory exhaustion**: Unconditional. A single session sending 30 unique `(from, to)` pairs/sec grows the HashMap by ~3 KB/sec. K=100 sessions → 300 KB/sec → ~1 GB/hour. No cleanup until disconnect.
- **Forwarding amplification**: Conditional on knowing connected peer IDs. With K sessions and known `to` peers, the victim forwards K×30 messages/sec to downstream peers, propagating load through the network.
- **`forward_rate_limiter` is completely ineffective** as a global cap — it only prevents replay of the *same* pair, which a rational attacker never repeats.

---

### Likelihood Explanation

- Requires only an unprivileged P2P connection — no authentication, no PoW, no stake.
- `from`/`to` PeerIds are arbitrary bytes; no binding to the actual session identity.
- Connected peer IDs are discoverable via the existing discovery/identify protocols.
- A single attacker node with K connections (bounded by the victim's `max_inbound`) can execute this continuously.

---

### Recommendation

1. **Add a global forwarding rate limiter** keyed by `session_id` (or source IP) rather than by attacker-controlled `(from, to)` fields. The per-session `rate_limiter` already exists — the `forward_rate_limiter` should be an additional *global* token bucket, not a per-pair one.
2. **Call `retain_recent()` periodically** (e.g., in the `notify` timer) rather than only on disconnect, to bound HashMap memory.
3. **Consider binding `from` to the actual sending session** via the identify protocol, so spoofed `from` values can be rejected.

---

### Proof of Concept

```
attacker controls 1 session to victim V

for i in 0..30:
    send ConnectionSync {
        from = random_peer_id_i,   # unique each time
        to   = known_connected_peer_id,
        route = [],
    }

# Each message:
#   rate_limiter.check_key((session_id, ConnectionSync_item_id)) → OK (30/sec budget)
#   forward_rate_limiter.check_key((from_i, to_id, item_id))    → OK (fresh bucket each time)
#   forward_sync(to_id) → sends message to connected peer

# Result: 30 forwarded messages/sec from 1 session
# forward_rate_limiter HashMap: 30 new entries, never pruned
# With K sessions: 30*K forwarded messages/sec, 30*K HashMap entries/sec
```

The invariant *"total forwarding throughput from a single attacker must be bounded regardless of (from,to) pair diversity"* is broken. The effective bound is 30×K (session rate limiter × session count), not a constant.

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

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L42-47)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
        Ok(SyncContent { route, from, to })
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L178-209)
```rust
    async fn forward_sync(&self, peer_id: &PeerId) -> Status {
        let target_sid = self
            .protocol
            .network_state
            .peer_registry
            .read()
            .get_key_by_peer_id(peer_id);

        match target_sid {
            Some(next_peer) => {
                let content = forward_sync(self.message);
                let new_message = packed::HolePunchingMessage::new_builder()
                    .set(content)
                    .build()
                    .as_bytes();
                let proto_id = SupportProtocols::HolePunching.protocol_id();
                debug!(
                    "forward the sync to next peer {} (id: {})",
                    next_peer, peer_id
                );
                if let Err(error) = self
                    .p2p_control
                    .send_message_to(next_peer, proto_id, new_message)
                    .await
                {
                    StatusCode::ForwardError.with_context(error)
                } else {
                    Status::ok()
                }
            }
            None => StatusCode::Ignore.with_context("the next peer in the route is disconnected"),
        }
```
