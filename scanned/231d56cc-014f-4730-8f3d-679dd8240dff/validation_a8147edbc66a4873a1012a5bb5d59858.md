Now I have all the information needed. Let me verify the `item_id()` return value to confirm it's a per-type constant, not per-message.

### Title
Unbounded `pending_delivered` Growth via Distinct `from` PeerIds in `ConnectionRequestProcess::respond_delivered` — (`network/src/protocols/hole_punching/mod.rs`, `connection_request.rs`)

---

### Summary

An unprivileged remote peer directly connected to a victim node can flood it with `ConnectionRequest` messages bearing distinct, attacker-chosen `from` peer IDs and `to = victim_peer_id`. The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)`, so every unique `from` is a fresh key that passes the limiter. Each such message inserts a new entry into `pending_delivered`. The map is only cleaned up in `notify()`, which fires every 5 minutes. The outer `rate_limiter` bounds growth to 30 entries/sec per session, but with multiple sessions the aggregate growth is multiplied.

---

### Finding Description

**Rate limiter architecture** (`network/src/protocols/hole_punching/mod.rs`):

- `rate_limiter`: keyed by `(PeerIndex, u32)` = `(session_id, item_type_id)`, quota 30/sec. This is the only per-session cap.
- `forward_rate_limiter`: keyed by `(PeerId, PeerId, u32)` = `(from, to, item_type_id)`, quota 1/sec per tuple. [1](#0-0) [2](#0-1) 

**Forward rate limiter bypass** (`connection_request.rs` lines 132–143): the check is `check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))`. Because `from` is attacker-supplied and arbitrary, each distinct `from` PeerId is a brand-new key — the limiter allows 1 msg/sec per key, but with N distinct keys it allows N msgs/sec total. [3](#0-2) 

**Insertion without bound** (`respond_delivered`, lines 161–237): the only deduplication guard checks `pending_delivered.get(&from_peer_id)` — it only blocks re-insertion for the *same* `from`. With distinct `from` IDs, every message inserts a new `(PeerId → (Vec<Multiaddr>, timestamp))` entry. [4](#0-3) [5](#0-4) 

**Cleanup only in `notify()`** which fires every `CHECK_INTERVAL = 5 minutes`. Entries with age < `TIMEOUT = 5 minutes` are retained, so in the worst case all entries inserted during a 5-minute window survive until the next tick. [6](#0-5) [7](#0-6) 

**Secondary growth**: `forward_rate_limiter`'s internal `HashMapStateStore` also accumulates one entry per unique `(from, to, item_id)` key. `retain_recent()` on it is only called in `disconnected()` — never periodically — so as long as the attacker holds the connection open, the limiter's own state also grows unboundedly. [8](#0-7) 

---

### Impact Explanation

Per session: 30 msgs/sec × 300 sec = **9,000 entries** per 5-minute window. Each entry holds a `PeerId` key (~39 bytes) plus a `Vec<Multiaddr>` of up to `ADDRS_COUNT_LIMIT = 24` addresses (~50 bytes each ≈ 1.2 KB) plus a `u64` timestamp. That is roughly **~11 MB per session per window**. [9](#0-8) 

With many inbound sessions (CKB nodes accept up to ~125 inbound peers by default), the aggregate reaches **~1.4 GB** before the first cleanup tick. Combined with the `forward_rate_limiter` state growth, this can exhaust heap memory, crash the node, and fragment the P2P network — causing consensus deviation by isolating the victim from block/tx propagation.

---

### Likelihood Explanation

- Requires only a single valid P2P connection (no privilege, no PoW, no key).
- `from` peer IDs are arbitrary bytes in the message payload; the attacker generates them locally.
- The attacker must supply at least one valid TCP IPv4/IPv6 listen address in the message (trivially satisfied).
- The `send_message_to` back to the attacker's session must succeed (it will, since the attacker is connected).
- No banning is triggered: `TooManyRequests` and `Ignore` statuses do not call `should_ban()`. [10](#0-9) [11](#0-10) 

---

### Recommendation

1. **Key `forward_rate_limiter` by `(session_id, to, item_id)` instead of `(from, to, item_id)`** — the session is the only attacker-uncontrollable identity. This makes the forward limiter equivalent to the outer limiter and eliminates the bypass.
2. **Cap `pending_delivered` size** with an LRU or a hard `HashMap::len()` guard before insertion.
3. **Periodically call `forward_rate_limiter.retain_recent()`** inside `notify()` alongside the existing `pending_delivered` cleanup, to prevent unbounded limiter state growth.
4. **Reduce `CHECK_INTERVAL`** or add an intermediate size-based eviction trigger.

---

### Proof of Concept

```rust
// Pseudocode unit test
let mut protocol = HolePunching::new(network_state);
let session_id = PeerIndex::new(1);
let victim_peer_id = protocol.network_state.local_peer_id().clone();

for i in 0..9000 {
    let from = PeerId::random(); // distinct each iteration
    let msg = build_connection_request(from, victim_peer_id.clone(), valid_tcp_addrs());
    // outer rate_limiter: 30/sec per (session_id, item_id) — passes
    // forward_rate_limiter: (from, to, item_id) — new key each time, passes
    // respond_delivered: pending_delivered.get(&from) → None → inserts
    protocol.received(ctx_with_session(session_id), msg).await;
}

assert_eq!(protocol.pending_delivered.len(), 9000); // grows proportionally
``` [12](#0-11) [13](#0-12)

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L25-30)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes

type PendingDeliveredInfo = (Vec<Multiaddr>, u64);
```

**File:** network/src/protocols/hole_punching/mod.rs (L44-46)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L145-166)
```rust
        if let Some(ban_time) = status.should_ban() {
            error!(
                "process {} from {}; ban {:?} since result is {}",
                item_name, session_id, ban_time, status
            );
            self.network_state.ban_session(
                &context.control().clone().into(),
                session_id,
                ban_time,
                status.to_string(),
            );
        } else if status.should_warn() {
            warn!(
                "process {} from {}; result is {}",
                item_name, session_id, status
            );
        } else if !status.is_ok() {
            debug!(
                "process {} from {}; result is {}",
                item_name, session_id, status
            );
        }
```

**File:** network/src/protocols/hole_punching/mod.rs (L172-175)
```rust
        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/mod.rs (L251-257)
```rust
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L155-239)
```rust
    async fn respond_delivered(
        &mut self,
        from_peer_id: PeerId,
        to_peer_id: &PeerId,
        remote_listens: Vec<Multiaddr>,
    ) -> Status {
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
        }
        let listen_addrs = {
            let public_addr = self.protocol.network_state.public_addrs(ADDRS_COUNT_LIMIT);
            if public_addr.len() < ADDRS_COUNT_LIMIT {
                let observed_addrs = self
                    .protocol
                    .network_state
                    .observed_addrs(ADDRS_COUNT_LIMIT - public_addr.len());
                let iter = public_addr
                    .iter()
                    .chain(observed_addrs.iter())
                    .map(Multiaddr::to_vec)
                    .map(|v| packed::Address::new_builder().bytes(v).build());
                packed::AddressVec::new_builder().extend(iter).build()
            } else {
                let iter = public_addr
                    .iter()
                    .map(Multiaddr::to_vec)
                    .map(|v| packed::Address::new_builder().bytes(v).build());
                packed::AddressVec::new_builder().extend(iter).build()
            }
        };
        let content = init_delivered(self.message, listen_addrs);
        let new_message = packed::HolePunchingMessage::new_builder()
            .set(content)
            .build()
            .as_bytes();
        let proto_id = SupportProtocols::HolePunching.protocol_id();

        let remote_listens: Vec<Multiaddr> = remote_listens
            .into_iter()
            .filter_map(|addr| match find_type(&addr) {
                TransportType::Memory
                | TransportType::Onion
                | TransportType::Ws
                | TransportType::Wss
                | TransportType::Tls => None,
                TransportType::Tcp => {
                    if addr
                        .iter()
                        .any(|p| matches!(p, Protocol::Ip4(_) | Protocol::Ip6(_)))
                    {
                        Some(addr)
                    } else {
                        None
                    }
                }
            })
            .collect();

        if remote_listens.is_empty() {
            return StatusCode::Ignore.with_context("remote listen address is empty");
        }

        debug!(
            "current peer is the target peer {}, send a response back",
            to_peer_id
        );

        if let Err(error) = self
            .p2p_control
            .send_message_to(self.peer, proto_id, new_message)
            .await
        {
            return StatusCode::ForwardError.with_context(error);
        }

        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));

        Status::ok()
```
