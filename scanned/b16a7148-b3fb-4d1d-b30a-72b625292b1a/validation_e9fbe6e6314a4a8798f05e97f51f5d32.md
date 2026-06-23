### Title
Unverified `from` Field in Hole-Punching `ConnectionRequest` Allows Peer Identity Forgery, Rate-Limit Bypass, and `pending_delivered` State Poisoning — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The hole-punching protocol's `ConnectionRequestProcess::execute()` reads the `from` peer ID exclusively from the attacker-controlled message body and never verifies it against the actual authenticated peer connection (`self.peer`). This is the direct CKB analog of the WETH `approve(address owner, …)` bug: just as WETH accepted the approver's identity as a caller-supplied parameter instead of `msg.sender`, CKB accepts the requester's identity as a message-supplied field instead of the verified session identity. Any connected peer can forge `from` to be any arbitrary `PeerId`, which (a) bypasses the `forward_rate_limiter` entirely by rotating through fake identities, and (b) poisons the `pending_delivered` map for any victim peer ID, silently blocking that victim's legitimate hole-punching requests through the relay node for up to two minutes.

---

### Finding Description

**Root cause — `from` is taken from message content, not from the authenticated session**

In `connection_request.rs`, `RequestContent::try_from` parses `from` directly out of the wire message:

```rust
let from = PeerId::from_bytes(value.from().raw_data().to_vec())...
``` [1](#0-0) 

`ConnectionRequestProcess` holds both the verified session handle (`self.peer: PeerIndex`) and the protocol state (`self.protocol`), which has access to `network_state.peer_registry`. The actual peer's `PeerId` is therefore reachable, but is never compared against `content.from`. There is no guard of the form `assert content.from == actual_peer_id`. [2](#0-1) 

**Attack surface 1 — `forward_rate_limiter` bypass**

The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)`:

```rust
if self.protocol.forward_rate_limiter
    .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
    .is_err()
{ return StatusCode::TooManyRequests... }
``` [3](#0-2) 

Because `content.from` is attacker-controlled, an attacker can rotate through an unlimited number of fake `from` peer IDs, each producing a fresh bucket in the rate limiter's `HashMapStateStore`. The limiter is configured at 1 request per second per `(from, to, item_id)` tuple: [4](#0-3) [5](#0-4) 

By cycling `from` values the attacker sends an unbounded stream of forwarded `ConnectionRequest` messages through any relay node, turning it into an amplifier toward any `to` peer.

**Attack surface 2 — `pending_delivered` state poisoning (direct DoS on victim)**

When the relay node is itself the `to` target (`self_peer_id == content.to`), it calls `respond_delivered(content.from, …)`: [6](#0-5) 

Inside `respond_delivered`, the node first checks `pending_delivered[from_peer_id]` and silently ignores any repeat within `HOLE_PUNCHING_INTERVAL` (2 minutes):

```rust
if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
    let now = unix_time_as_millis();
    if now - t < HOLE_PUNCHING_INTERVAL {
        return StatusCode::Ignore...
    }
}
...
self.protocol.pending_delivered.insert(from_peer_id, (remote_listens, now));
``` [7](#0-6) 

An attacker connected to relay node R sends a `ConnectionRequest` with `from = victim_peer_id` and `to = R_peer_id`. R inserts `pending_delivered[victim_peer_id] = (attacker_addrs, now)`. For the next 2 minutes, any legitimate `ConnectionRequest` from the real victim to R is silently dropped with `StatusCode::Ignore`. The victim receives no error; the hole-punching attempt simply fails.

The same unverified-`from` pattern is present in `ConnectionRequestDelivered` and `ConnectionSync` handlers: [8](#0-7) [9](#0-8) 

---

### Impact Explanation

1. **Rate-limit bypass / relay amplification**: A single connected attacker can flood any target peer with an unbounded volume of forwarded `ConnectionRequest` messages by rotating fake `from` IDs, defeating the only per-flow forwarding throttle.
2. **Targeted DoS on hole-punching**: By forging `from = victim_peer_id` toward any relay node the attacker is connected to, the attacker silently blocks the victim's NAT traversal through that relay for 2-minute windows, renewable indefinitely. Victims behind NAT who depend on hole-punching for connectivity are effectively isolated from the network.
3. **`inflight_requests` cancellation** (secondary): In `ConnectionRequestDelivered`, when `self_peer_id == content.from`, the node removes `inflight_requests[content.to]`. An attacker who knows the relay's public peer ID can cancel any of its in-flight hole-punching sessions. [10](#0-9) 

---

### Likelihood Explanation

- **Precondition**: The attacker needs only a single authenticated P2P connection to any CKB node that has the `HolePunching` protocol enabled. No keys, no privileged access, no 51% hash power.
- **Peer IDs are public**: Every node broadcasts its peer ID via the identify protocol, so the attacker can trivially learn both the relay's and the victim's peer IDs.
- **No cryptographic barrier**: The `from` field is a raw byte sequence; forging it requires no key material.
- **Hole-punching is enabled by default** in the network config when the feature is compiled in. [11](#0-10) 

---

### Recommendation

In `ConnectionRequestProcess::execute()`, after parsing `content`, look up the actual `PeerId` of `self.peer` from the peer registry and assert it equals `content.from`:

```rust
let actual_from = self.protocol.network_state
    .with_peer_registry(|reg| reg.get_peer(self.peer).map(|p| p.peer_id.clone()));
match actual_from {
    Some(id) if id == content.from => { /* proceed */ }
    _ => return StatusCode::InvalidFromPeerId.with_context("from does not match sender"),
}
```

Apply the same fix to `ConnectionRequestDeliveredProcess` and `ConnectionSyncProcess`. The `forward_rate_limiter` key should then use the verified session identity (`self.peer`) rather than the message-supplied `content.from`, or the `from` field should be dropped from the message entirely and reconstructed from the session on each hop. [12](#0-11) 

---

### Proof of Concept

**Setup**: Attacker A is connected to relay node R. Victim V is behind NAT and depends on hole-punching through R.

**Step 1 — `pending_delivered` poisoning**:
1. A learns R's peer ID (`R_id`) from the identify protocol.
2. A learns V's peer ID (`V_id`) from the identify protocol or gossip.
3. A sends a `ConnectionRequest` message to R with `from = V_id`, `to = R_id`, `listen_addrs = [A's address / V_id]`, `max_hops = 1`.
4. R parses `content.from = V_id`, passes the `forward_rate_limiter` check (fresh bucket), calls `respond_delivered(V_id, …)`, and inserts `pending_delivered[V_id] = (A_addrs, now)`.

**Step 2 — Victim is blocked**:
5. V sends a legitimate `ConnectionRequest` to R with `from = V_id`, `to = R_id`.
6. R calls `respond_delivered(V_id, …)`, finds `pending_delivered[V_id]` with timestamp < 2 minutes ago, returns `StatusCode::Ignore`. V's request is silently dropped.

**Step 3 — Rate-limit bypass for amplification**:
7. A sends `ConnectionRequest` messages to R with `from = random_peer_id_1`, `from = random_peer_id_2`, … each targeting `to = V_id`.
8. Each message creates a new bucket in `forward_rate_limiter`, bypassing the 1 req/s cap. R forwards all of them toward V, flooding V with `ConnectionRequestDelivered` messages.

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L85-108)
```rust
pub(crate) struct ConnectionRequestProcess<'a> {
    message: packed::ConnectionRequestReader<'a>,
    protocol: &'a mut HolePunching,
    peer: PeerIndex,
    p2p_control: &'a ServiceAsyncControl,
    msg_item_id: u32,
}

impl<'a> ConnectionRequestProcess<'a> {
    pub(crate) fn new(
        message: packed::ConnectionRequestReader<'a>,
        protocol: &'a mut HolePunching,
        peer: PeerIndex,
        p2p_control: &'a ServiceAsyncControl,
        msg_item_id: u32,
    ) -> Self {
        Self {
            message,
            protocol,
            peer,
            p2p_control,
            msg_item_id,
        }
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-237)
```rust
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
```

**File:** network/src/protocols/hole_punching/mod.rs (L38-47)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L150-160)
```rust
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
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L85-95)
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
