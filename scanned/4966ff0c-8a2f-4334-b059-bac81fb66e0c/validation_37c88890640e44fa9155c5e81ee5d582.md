### Title
Unauthenticated `from` Field in `ConnectionRequest` Allows Any Peer to Poison `pending_delivered` Cooldown, Blocking Hole Punching for Arbitrary Victims — (`File: network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The `HolePunching` protocol's `respond_delivered` function updates a per-`from`-peer-ID cooldown timestamp in `pending_delivered` using the `from` field taken directly from the message payload. Because the `from` field is never verified against the actual session peer ID, any connected peer can spoof `from` to be any victim's peer ID, poisoning the cooldown entry on a relay node and blocking that relay from responding to legitimate hole-punching requests from the victim for 2 minutes per injection. Repeated injections permanently deny hole-punching service for the victim through any relay the attacker is connected to.

---

### Finding Description

`HolePunching` maintains a `pending_delivered: HashMap<PeerId, PendingDeliveredInfo>` map keyed by the `from` peer ID of a `ConnectionRequest`. [1](#0-0) 

When a relay node receives a `ConnectionRequest` whose `to` field equals its own peer ID, it calls `respond_delivered` with `content.from` as the `from_peer_id`: [2](#0-1) 

Inside `respond_delivered`, the cooldown check gates the response: [3](#0-2) 

If the check passes, the function writes the current timestamp into `pending_delivered` under `from_peer_id`: [4](#0-3) 

The `HOLE_PUNCHING_INTERVAL` cooldown is 2 minutes: [5](#0-4) 

The `RequestContent::try_from` parser validates only that `from` is syntactically valid bytes for a `PeerId`; it never checks that `from` matches the actual session peer ID: [6](#0-5) 

There is no binding between the authenticated transport-layer session identity and the `from` field in the message payload.

---

### Impact Explanation

An attacker who connects to any relay node can send a `ConnectionRequest` with `to = relay_node_peer_id` and `from = victim_peer_id`. The relay node will:

1. Pass the `forward_rate_limiter` check (1 req/sec per `(from, to)` pair — trivially satisfied).
2. Find no existing `pending_delivered` entry for `victim_peer_id` on the first call.
3. Successfully send a `ConnectionRequestDelivered` response back to the attacker's session.
4. Write `(victim_peer_id, now)` into `pending_delivered`.

For the next 2 minutes, any legitimate `ConnectionRequest` from the real victim peer to this relay node will hit the cooldown check and receive `StatusCode::Ignore`, silently dropping the request. The attacker refreshes the poison entry every ~2 minutes with a single message per relay node. By targeting multiple relay nodes simultaneously, the attacker can deny hole-punching service for the victim across the entire reachable relay graph, preventing NAT traversal and effectively isolating the victim from peers reachable only via hole punching.

---

### Likelihood Explanation

The attack requires only a standard P2P connection to any relay node — no privileged access, no keys, no special role. The `from` field is fully attacker-controlled with no cryptographic binding. The `forward_rate_limiter` (1 req/sec per `(from, to)` pair) does not impede the attack since only one message per 2-minute window is needed per relay. The attacker can target any known peer ID (peer IDs are publicly advertised in the P2P discovery protocol). This is a low-cost, persistent, remotely triggerable DoS.

---

### Recommendation

Bind the `from` field to the authenticated session identity. In `ConnectionRequestProcess::execute`, before calling `respond_delivered`, verify that `content.from` equals the peer ID of the actual session (`context.session.id` resolved to its `PeerId` via the peer registry). Reject messages where `content.from` does not match the session's authenticated peer ID. This mirrors the fix recommended in the BORG_SAFE report: restrict the state-mutating path to the legitimately authenticated caller only.

---

### Proof of Concept

1. Attacker connects to relay node R (session established, attacker's peer ID = `A`).
2. Attacker sends a `ConnectionRequest` message with `from = V` (victim's peer ID) and `to = R` (relay's own peer ID), with valid `listen_addrs` containing at least one TCP/IP address.
3. R's `received` handler dispatches to `ConnectionRequestProcess::execute`.
4. `content.to == self_peer_id` → `respond_delivered(V, R, listen_addrs)` is called.
5. `pending_delivered.get(&V)` returns `None` (first call) → cooldown check passes.
6. R sends `ConnectionRequestDelivered` back to attacker's session.
7. R inserts `(V, now)` into `pending_delivered`.
8. Victim V now sends a legitimate `ConnectionRequest` with `from = V`, `to = R`.
9. R's `respond_delivered` checks `pending_delivered.get(&V)` → finds timestamp `now`, `now - t < HOLE_PUNCHING_INTERVAL` → returns `StatusCode::Ignore`.
10. Victim's hole-punching attempt through R is silently dropped.
11. Attacker repeats step 2 every 119 seconds to maintain the block indefinitely. [7](#0-6) [8](#0-7)

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L24-24)
```rust
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L30-44)
```rust
type PendingDeliveredInfo = (Vec<Multiaddr>, u64);
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L35-38)
```rust
    fn try_from(value: &packed::ConnectionRequestReader<'_>) -> Result<Self, Self::Error> {
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L110-153)
```rust
    pub(crate) async fn execute(mut self) -> Status {
        let content = match RequestContent::try_from(&self.message) {
            Ok(content) => content,
            Err(status) => return status,
        };
        if content.listen_addrs.len() > ADDRS_COUNT_LIMIT || content.listen_addrs.is_empty() {
            return StatusCode::InvalidListenAddrLen
                .with_context("the listen address count is too large or empty");
        }

        if content.max_hops > MAX_HOPS {
            return StatusCode::InvalidMaxTTL.into();
        }
        if content.route.len() > MAX_HOPS as usize {
            return StatusCode::InvalidRoute.with_context("the route length is too long");
        }

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

        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
        } else if content.max_hops == 0u8 {
            StatusCode::ReachedMaxHops.into()
        } else {
            self.forward_message(self_peer_id, &content.to).await
        }
    }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-167)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```
