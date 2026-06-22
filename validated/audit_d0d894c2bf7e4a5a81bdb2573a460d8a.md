The code is confirmed. Let me verify the `notify()` handler does not call `retain_recent()` and check the governor crate's behavior.

### Title
Unbounded `forward_rate_limiter` HashMapStateStore Growth via Persistent Connection Sending Distinct `(from, to)` PeerId Pairs — (`network/src/protocols/hole_punching/mod.rs`)

---

### Summary

The `HolePunching` protocol's `forward_rate_limiter` (`RateLimiter<(PeerId, PeerId, u32)>` backed by `governor::state::keyed::HashMapStateStore`) accumulates one new HashMap entry per unique `(from, to, msg_item_id)` triple seen in forwarded messages. The only call to `retain_recent()` — which evicts stale entries — is inside `disconnected()`. The periodic `notify()` handler (fired every 5 minutes) never calls `retain_recent()`. An attacker who maintains a persistent TCP connection and sends `ConnectionSync` messages with distinct attacker-chosen `(from, to)` PeerId pairs causes the HashMap to grow without bound, exhausting node memory.

---

### Finding Description

**Entrypoint:** Any peer connected over P2P can send `HolePunchingMessage::ConnectionSync` messages.

**Outer rate limiter** (`rate_limiter`, keyed by `(session_id, msg_item_id)`): allows 30 `ConnectionSync` messages per second per session. [1](#0-0) 

Each of those 30 messages passes a distinct attacker-chosen `(from, to)` pair into `forward_rate_limiter.check_key(...)`: [2](#0-1) 

`check_key` on a `HashMapStateStore` inserts a new keyed bucket for every previously-unseen key. The `forward_rate_limiter` is declared as: [3](#0-2) 

`retain_recent()` — the only mechanism to evict stale entries — is called **only** in `disconnected()`: [4](#0-3) 

The `notify()` handler, which fires every 5 minutes, cleans up `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on either rate limiter: [5](#0-4) 

As long as the attacker does not disconnect, the HashMap grows at up to 30 entries/second indefinitely.

---

### Impact Explanation

Each `(PeerId, PeerId, u32)` entry in the HashMap stores two `PeerId` values (each ~39 bytes for a multihash-encoded Ed25519 key) plus governor's internal rate-limiter state. At 30 insertions/second sustained over hours or days, memory consumption grows without bound. A single malicious peer can exhaust the heap of any CKB node that has the `HolePunching` protocol enabled, crashing it. Because `ConnectionSync` messages are forwarded along relay paths, a single attacker session can trigger growth on every relaying node simultaneously.

---

### Likelihood Explanation

The `HolePunching` protocol is enabled by default when `SupportProtocol::HolePunching` is in the config. [6](#0-5) 

No authentication or privilege is required — any peer that establishes a TCP connection can send `ConnectionSync`. The attacker only needs to generate valid `PeerId` bytes (any valid multihash), which is trivial. The outer per-session rate limit of 30/sec is not a meaningful barrier; it merely sets the growth rate.

---

### Recommendation

Call `self.rate_limiter.retain_recent()` and `self.forward_rate_limiter.retain_recent()` inside the `notify()` handler, which already fires on a 5-minute `CHECK_INTERVAL` timer. This ensures stale entries are periodically evicted regardless of whether any peer disconnects:

```rust
async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
    self.rate_limiter.retain_recent();           // add this
    self.forward_rate_limiter.retain_recent();   // add this
    // ... existing cleanup of pending_delivered and inflight_requests
}
``` [7](#0-6) 

---

### Proof of Concept

1. Connect to a CKB node with `HolePunching` enabled.
2. In a loop, send `ConnectionSync` messages at 30/sec, each with a freshly generated random `from` and `to` PeerId (valid multihash bytes).
3. Never disconnect.
4. Observe `HolePunching` struct heap size (via `/proc/<pid>/status` `VmRSS`) growing linearly with the number of messages sent, with no plateau, confirming O(N) unbounded growth.
5. After ~10^6 distinct pairs (~9 hours at 30/sec), the node's memory is exhausted and it crashes.

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

**File:** network/src/protocols/hole_punching/mod.rs (L169-244)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let status = self.network_state.connection_status();

        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);

        if status.non_whitelist_outbound < status.max_outbound && status.total > 0 {
            let target = &self.network_state.required_flags;
            let addrs = self.network_state.with_peer_store_mut(|p| {
                p.fetch_nat_addrs(
                    (status.max_outbound - status.non_whitelist_outbound) as usize,
                    *target,
                )
            });

            let from_peer_id = self.network_state.local_peer_id();
            let listen_addrs = {
                let public_addr = self.network_state.public_addrs(ADDRS_COUNT_LIMIT);
                if public_addr.len() < ADDRS_COUNT_LIMIT {
                    let observed_addrs = self
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

            let mut inflight = Vec::new();
            for i in addrs {
                if let Some(to_peer_id) = extract_peer_id(&i.addr) {
                    let conn_req = {
                        let content = component::init_request(
                            from_peer_id,
                            &to_peer_id,
                            listen_addrs.clone(),
                        );
                        packed::HolePunchingMessage::new_builder()
                            .set(content)
                            .build()
                    };
                    let proto_id = SupportProtocols::HolePunching.protocol_id();

                    // Broadcast to a number of nodes equal to the square root of the total connection count using gossip.
                    let mut total = status.total.isqrt();
                    let _ignore = context
                        .filter_broadcast(
                            TargetSession::Filter(Box::new(move |_| {
                                total = total.saturating_sub(1);
                                total != 0
                            })),
                            proto_id,
                            conn_req.as_bytes(),
                        )
                        .await;
                    inflight.push(to_peer_id);
                }
            }

            let now = unix_time_as_millis();
            for peer_id in inflight {
                self.inflight_requests.insert(peer_id, now);
            }
        }
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

**File:** network/src/network.rs (L940-953)
```rust
        // HolePunching protocol
        #[cfg(not(target_family = "wasm"))]
        if config
            .support_protocols
            .contains(&SupportProtocol::HolePunching)
        {
            let hole_punching_state = Arc::clone(&network_state);
            let hole_punching_meta =
                SupportProtocols::HolePunching.build_meta_with_service_handle(move || {
                    ProtocolHandle::Callback(Box::new(
                        crate::protocols::hole_punching::HolePunching::new(hole_punching_state),
                    ))
                });
            protocol_metas.push(hole_punching_meta);
```
