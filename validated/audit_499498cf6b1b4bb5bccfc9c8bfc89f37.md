### Title
Unverified `from` Field in `ConnectionRequest` Allows Forged Sender Identity and Rate-Limiter Bypass - (`File: network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The `ConnectionRequestProcess::execute()` handler in CKB's hole-punching protocol accepts the `from` field of a `ConnectionRequest` message as the sender's identity without ever verifying it against the actual peer ID of the connected session. Any unprivileged peer can forge an arbitrary `from` value, bypassing the `forward_rate_limiter` and poisoning the `pending_delivered` map with attacker-controlled listen addresses.

---

### Finding Description

The `ConnectionRequest` molecule message carries a `from: Bytes` field that is supposed to identify the originating peer. [1](#0-0) 

When a node receives this message, `ConnectionRequestProcess::execute()` parses `content.from` from the wire bytes and uses it in two security-sensitive operations:

**1. The `forward_rate_limiter` key:** [2](#0-1) 

**2. The `pending_delivered` map insertion (when the node is the target):** [3](#0-2) 

At no point is `content.from` compared against the actual peer ID of the session (`self.peer` / `context.session.id`). The only validation performed on `from` is that it is a syntactically valid `PeerId` and that any embedded peer ID in `listen_addrs` matches it — but both of those fields are fully attacker-controlled: [4](#0-3) 

The legitimate node sends `ConnectionRequest` with its own real peer ID as `from`: [5](#0-4) 

But nothing in the receiving handler enforces this invariant.

The per-session `rate_limiter` (keyed by `(session_id, msg_item_id)`) does bound the raw message rate per connection: [6](#0-5) 

However, the `forward_rate_limiter` is a separate, independent limiter keyed by `(from, to, msg_item_id)` and is specifically designed to prevent the same `(from, to)` pair from being forwarded more than once per second: [7](#0-6) 

By cycling through distinct forged `from` values, an attacker exhausts unique `(from, to)` keys and bypasses this limiter entirely.

---

### Impact Explanation

**Impact 1 — Forward rate-limiter bypass / P2P amplification:**

The `forward_rate_limiter` is the only mechanism preventing a single peer from causing a node to gossip-broadcast many unique `ConnectionRequest` messages. With `from` forged to a new random peer ID on each message, every message is treated as a fresh `(from, to)` pair and forwarded. The node gossip-broadcasts each forwarded message to `sqrt(total_peers)` peers: [8](#0-7) 

This creates a network-level amplification: one attacker connection → up to 30 messages/second (per-session cap) → each forwarded to `sqrt(N)` peers → cascading across the network.

**Impact 2 — `pending_delivered` poisoning with attacker-controlled addresses:**

When the receiving node is the intended target (`self_peer_id == &content.to`), `respond_delivered()` stores the attacker-supplied `listen_addrs` into `pending_delivered` keyed by the forged `from_peer_id`: [9](#0-8) 

The `pending_delivered` map is subsequently consumed during `ConnectionSync` processing (in `connection_sync.rs`) to initiate outbound TCP NAT-traversal connections. By forging `from` as a legitimate peer's ID and supplying attacker-controlled `listen_addrs`, the attacker causes the victim node to attempt TCP connections to arbitrary IP addresses — a forced-outbound-connection primitive. This can be used to:
- Probe internal network topology from the victim node
- Exhaust the victim's outbound connection slots
- Facilitate eclipse-attack setup by directing the victim toward attacker-controlled peers

---

### Likelihood Explanation

The HolePunching protocol is a standard supported protocol open to any connected peer: [10](#0-9) 

No authentication, stake, or special role is required. Any peer that establishes a TCP session and opens the `HolePunching` sub-protocol can send a `ConnectionRequest` with an arbitrary `from` field. The attack requires only a single inbound or outbound connection to the victim node.

---

### Recommendation

In `ConnectionRequestProcess::execute()`, after parsing `content.from`, resolve the actual peer ID of the sending session from the peer registry and assert equality:

```rust
// Resolve the actual peer ID of the sender from the session
let actual_from = self
    .protocol
    .network_state
    .peer_registry
    .read()
    .get_peer_id_by_session(self.peer);

match actual_from {
    Some(ref actual_id) if actual_id == &content.from => { /* proceed */ }
    _ => return StatusCode::InvalidFromPeerId
             .with_context("from field does not match actual sender peer id"),
}
```

Apply the same check symmetrically in `ConnectionRequestDeliveredProcess` and `ConnectionSyncProcess` for their respective `from`/`to` fields where the current node is an endpoint.

---

### Proof of Concept

1. Attacker establishes a connection to victim CKB node `V` (peer ID `V_id`) and opens the `HolePunching` protocol.
2. Attacker sends 30 `ConnectionRequest` messages per second (per-session cap), each with a freshly generated random `from` peer ID and `to = V_id`, and `listen_addrs` pointing to attacker-controlled IPs.
3. For each message where `to == V_id`, `respond_delivered()` fires: `pending_delivered[random_from] = (attacker_ips, now)`.
4. For messages where `to != V_id`, `forward_message()` fires: the node gossip-broadcasts to `sqrt(N)` peers. Because each `(random_from, to)` pair is unique, `forward_rate_limiter` never triggers.
5. Attacker then sends a crafted `ConnectionSync` message (or waits for the protocol to consume `pending_delivered`) causing `V` to initiate TCP connections to attacker-controlled IPs. [11](#0-10)

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L35-61)
```rust
    fn try_from(value: &packed::ConnectionRequestReader<'_>) -> Result<Self, Self::Error> {
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
        let listen_addrs: Vec<Multiaddr> = value
            .listen_addrs()
            .iter()
            .map(
                |raw| match Multiaddr::try_from(raw.bytes().raw_data().to_vec()) {
                    Ok(mut addr) => {
                        if let Some(peer_id) = extract_peer_id(&addr) {
                            if peer_id != from {
                                return Err(StatusCode::InvalidListenAddrLen
                                    .with_context("peer id in listen address is invalid"));
                            }
                        } else {
                            addr.push(Protocol::P2P(Cow::Borrowed(from.as_bytes())));
                        }
                        Ok(addr)
                    }
                    Err(_) => Err(StatusCode::InvalidListenAddrLen
                        .with_context("the listen address is invalid")),
                },
            )
            .collect::<Result<Vec<_>, _>>()?;
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L155-237)
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
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L279-305)
```rust
                // Broadcast to a number of nodes equal to the square root of the total connection count using gossip.
                let sid = self.peer;
                let mut total = self
                    .protocol
                    .network_state
                    .with_peer_registry(|p| p.peers().len())
                    .isqrt();
                if let Err(error) = self
                    .p2p_control
                    .filter_broadcast(
                        TargetSession::Filter(Box::new(move |id| {
                            if id == &sid {
                                return false;
                            }
                            total = total.saturating_sub(1);
                            total != 0
                        })),
                        proto_id,
                        new_message,
                    )
                    .await
                {
                    StatusCode::BroadcastError.with_context(error)
                } else {
                    Status::ok()
                }
            }
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L158-169)
```rust
pub(crate) fn init_request(
    from: &PeerId,
    to: &PeerId,
    listen_addrs: packed::AddressVec,
) -> packed::ConnectionRequest {
    packed::ConnectionRequest::new_builder()
        .from(from.as_bytes())
        .to(to.as_bytes())
        .max_hops(MAX_HOPS)
        .listen_addrs(listen_addrs)
        .build()
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

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/support_protocols.rs (L55-57)
```rust
    /// HolePunching: A protocol used to connect peers behind firewalls or NAT routers.
    HolePunching,
}
```
