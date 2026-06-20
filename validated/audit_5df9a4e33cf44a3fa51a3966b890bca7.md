### Title
Unauthenticated `from` Field in `ConnectionRequest` Enables `pending_delivered` Cache Poisoning — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The `from` field in a `ConnectionRequest` message is accepted verbatim from message bytes with no verification that it matches the actual sending peer's session identity. This allows any connected peer to poison the victim's `pending_delivered` cache with attacker-controlled listen addresses, causing the victim to attempt NAT traversal to attacker-controlled endpoints when the legitimate `ConnectionSync` arrives.

---

### Finding Description

`pending_delivered` is a `HashMap<PeerId, PendingDeliveredInfo>` on the `HolePunching` struct, keyed by the `from` PeerId from the message content. [1](#0-0) 

When the victim node is the `to` target of a `ConnectionRequest`, `respond_delivered` is called with `content.from` (parsed from message bytes) as the cache key: [2](#0-1) 

Inside `respond_delivered`, the only guard against overwriting an existing entry is a time-window check against `HOLE_PUNCHING_INTERVAL` (2 minutes): [3](#0-2) 

If the interval has elapsed, the entry is unconditionally overwritten with the attacker-supplied `remote_listens`: [4](#0-3) 

At no point is `content.from` compared against `self.peer` (the actual session peer index). The `self.peer` value is used only to route the `ConnectionRequestDelivered` response back: [5](#0-4) 

The `listen_addrs` validation only checks that any embedded peer ID matches `content.from` — since the attacker controls `content.from`, they set it to `real_from` and supply attacker-controlled IP addresses (the code appends `real_from`'s peer ID automatically to addresses lacking one): [6](#0-5) 

When the legitimate `ConnectionSync{from=real_from}` subsequently arrives, `ConnectionSyncProcess::execute` looks up `pending_delivered[real_from]` and spawns `try_nat_traversal` against whatever addresses are stored there: [7](#0-6) 

---

### Impact Explanation

The victim node attempts TCP hole-punching connections to attacker-controlled endpoints instead of the real peer's addresses. Because CKB peer identity is cryptographically bound (PeerId is derived from a public key), the attacker cannot impersonate `real_from` at the TLS/noise layer — the connection attempt will fail or connect to an unrelated peer. The concrete impact is:

1. **NAT traversal disruption**: the legitimate hole-punching window is consumed connecting to wrong addresses; the real peer misses the synchronised traversal attempt.
2. **Amplified network churn**: the attacker can repeat this for any known `(from, to)` pair every 2 minutes at negligible cost, systematically preventing NAT-traversed connections across the network.
3. **Victim resource waste**: each poisoned entry causes the victim to spawn async TCP connection tasks to attacker-controlled IPs.

---

### Likelihood Explanation

- PeerIds are public (advertised in the peer store and gossip).
- The attacker needs only one live P2P session to any node in the network; the `ConnectionRequest` is gossiped via `filter_broadcast` and will reach the victim.
- The per-session rate limiter (`session_id, item_id`) and the `forward_rate_limiter` (`from, to, item_id`) do not prevent the attack — one message per 2-minute window per `(from, to)` pair is sufficient. [8](#0-7) [9](#0-8) 

---

### Recommendation

Authenticate the `from` field against the actual session peer identity before writing to `pending_delivered`. Concretely:

1. Resolve the actual PeerId of `self.peer` from the peer registry.
2. In `respond_delivered`, assert `actual_session_peer_id == from_peer_id`; reject with `StatusCode::Ignore` (or ban) if they differ.
3. Alternatively, key `pending_delivered` by the actual session peer ID rather than the message-supplied `from` field.

---

### Proof of Concept

**State-transition sequence:**

1. Legitimate flow: victim receives `ConnectionRequest{from=real_from, to=victim, listen_addrs=[real_addrs]}` → `pending_delivered[real_from] = ([real_addrs], t0)`.
2. Attacker waits `HOLE_PUNCHING_INTERVAL` (2 min, defined at `mod.rs:24`). [10](#0-9) 
3. Attacker (connected to any network peer) sends `ConnectionRequest{from=real_from, to=victim, listen_addrs=[attacker_ip:port]}`. The message is gossiped until it reaches the victim.
4. Victim's `respond_delivered` finds `now - t0 >= HOLE_PUNCHING_INTERVAL`, passes the guard, and executes `pending_delivered.insert(real_from, ([attacker_ip:port/p2p/real_from], t1))`. [11](#0-10) 
5. Legitimate `ConnectionSync{from=real_from, to=victim}` arrives (routed normally).
6. `ConnectionSyncProcess::execute` reads `pending_delivered[real_from]` → `[attacker_ip:port/p2p/real_from]`, spawns `try_nat_traversal` to attacker-controlled endpoint. [12](#0-11) 

**Assertion violated:** `try_nat_traversal` is called with `attacker_addrs`, not `real_addrs`. The legitimate NAT traversal window is lost.

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L47-54)
```rust
                        if let Some(peer_id) = extract_peer_id(&addr) {
                            if peer_id != from {
                                return Err(StatusCode::InvalidListenAddrLen
                                    .with_context("peer id in listen address is invalid"));
                            }
                        } else {
                            addr.push(Protocol::P2P(Cow::Borrowed(from.as_bytes())));
                        }
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L226-232)
```rust
        if let Err(error) = self
            .p2p_control
            .send_message_to(self.peer, proto_id, new_message)
            .await
        {
            return StatusCode::ForwardError.with_context(error);
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-128)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());

                    match listens_info {
                        Some(listens) => {
                            let tasks = listens
                                .into_iter()
                                .map(|listen_addr| {
                                    Box::pin(try_nat_traversal(self.bind_addr, listen_addr))
                                })
                                .collect::<Vec<_>>();

                            if tasks.is_empty() {
                                return StatusCode::Ignore.with_context("no valid listen address");
                            }
```
