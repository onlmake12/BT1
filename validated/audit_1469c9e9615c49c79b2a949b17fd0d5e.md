### Title
Unbounded `forward_rate_limiter` HashMapStateStore Growth via Attacker-Controlled `(from, to)` PeerIds in Hole-Punching Messages — (`network/src/protocols/hole_punching/mod.rs`)

---

### Summary

The `HolePunching` protocol handler maintains a `forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>` backed by `governor::state::keyed::HashMapStateStore`. The key includes `content.from` and `content.to` — both fully attacker-controlled fields from the message body. The `retain_recent()` cleanup method is called **only** in `disconnected()`, never during message processing or in the periodic `notify()` timer. An attacker who maintains a persistent session and sends messages with unique `(from, to)` pairs at the outer rate limit (30/sec) causes the internal HashMap to grow without bound, exhausting heap memory.

---

### Finding Description

**Outer rate limiter key** (line 97): `(session_id, msg.item_id())`. The `item_id()` is a fixed enum discriminant — 0 for `ConnectionRequest`, 1 for `ConnectionRequestDelivered`, 2 for `ConnectionSync` — confirmed by the generated code. This limits to 30 messages/sec per `(session, message_type)` pair. [1](#0-0) [2](#0-1) 

**Inner `forward_rate_limiter` key** (line 135): `(content.from, content.to, self.msg_item_id)`. The `from` and `to` fields are raw `Bytes` from the molecule-encoded message body, parsed into `PeerId`. An attacker freely varies these per message. [3](#0-2) [4](#0-3) 

**`retain_recent()` is called only on disconnect**, not in `notify()` or `received()`. The `notify()` handler (firing every 5 minutes) cleans up `pending_delivered` and `inflight_requests` but never touches either rate limiter. [5](#0-4) [6](#0-5) 

**`forward_rate_limiter` quota** is 1/sec per key (line 256). The first `check_key()` call for any new `(from, to, item_id)` triple always succeeds (no prior state), inserting a new entry. The attacker never reuses a `(from, to)` pair, so every message passes and inserts. [7](#0-6) 

**The `HashMapStateStore` type** is declared at lines 31–35: [8](#0-7) 

---

### Impact Explanation

Each `(from, to)` entry in the `HashMapStateStore` stores two `PeerId` values (~32–39 bytes each) plus a `u32` plus governor internal state (~16 bytes) — roughly 90–100 bytes per entry. At 30 entries/sec from a single session:

- After 1 hour: ~108,000 entries ≈ ~10 MB
- After 24 hours: ~2.6M entries ≈ ~240 MB
- With N concurrent sessions: scales linearly

Since the `HolePunching` protocol handler is a single shared instance (one `HolePunching` struct per node, not per session), all sessions write into the same `forward_rate_limiter`. With the default max peer count, an attacker controlling multiple sessions multiplies the growth rate proportionally. Sustained over hours, this exhausts heap memory and crashes the node process.

---

### Likelihood Explanation

- The attacker needs only a standard P2P connection — no privileges, no PoW, no keys.
- The `HolePunching` protocol is registered as a standard protocol in `network.rs` when `SupportProtocol::HolePunching` is in the config.
- The attack requires only that the attacker not disconnect, which is trivially maintained.
- The outer rate limiter (30/sec) does not prevent the attack; it merely sets the growth rate.
- The `route` self-check at line 128 fires before the `forward_rate_limiter` check, but the attacker simply omits their own peer ID from the route field to reach the vulnerable code path. [9](#0-8) [10](#0-9) 

---

### Recommendation

Call `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify()` handler, which already fires on a 5-minute `CHECK_INTERVAL`. This bounds the map size to entries active within the last rate-limit window, regardless of whether peers disconnect. [11](#0-10) [6](#0-5) 

---

### Proof of Concept

```
1. Connect to a target CKB node that has HolePunching enabled.
2. In a loop (rate-limited to 30/sec to stay under the outer limiter):
   a. Generate a fresh random PeerId for `from` and a fresh random PeerId for `to`.
   b. Construct a valid ConnectionRequest molecule message:
      - from = random_peer_id_A
      - to   = random_peer_id_B
      - max_hops = 1
      - route = [] (empty, so self-peer-id check is skipped)
      - listen_addrs = [one valid multiaddr]
   c. Send the message over the HolePunching protocol stream.
3. Never disconnect.
4. After T seconds, the forward_rate_limiter HashMapStateStore contains
   30*T entries. Assert map size grows linearly with T and is not bounded
   by the number of active sessions.
5. After sufficient time (hours), the node process OOMs and crashes.
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L25-26)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
```

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

**File:** network/src/protocols/hole_punching/mod.rs (L940-953)
```rust

```

**File:** util/gen-types/src/generated/protocols.rs (L5548-5554)
```rust
    pub fn item_id(&self) -> molecule::Number {
        match self {
            HolePunchingMessageUnion::ConnectionRequest(_) => 0,
            HolePunchingMessageUnion::ConnectionRequestDelivered(_) => 1,
            HolePunchingMessageUnion::ConnectionSync(_) => 2,
        }
    }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L127-143)
```rust
        let self_peer_id = self.protocol.network_state.local_peer_id();
        if content.route.contains(self_peer_id) {
            return StatusCode::Ignore.with_context("the message is passed, ignore it");
        }

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

**File:** util/gen-types/schemas/protocols.mol (L94-105)
```text
table ConnectionRequest {
    // Peer Id.
    from: Bytes,
    // Peer Id.
    to: Bytes,
    // Limit the max count of hops: the max count of peers in the message delivery route.
    max_hops: byte,
    // The message delivery route (type: `Vec<PeerId>`).
    route: BytesVec,
    // These are the addresses on which the "from" peer is listening as multi-addresses.
    listen_addrs: AddressVec,
}
```
