### Title
Unverified `from` Peer ID in `ConnectionRequest` Allows Sender Impersonation in Hole Punching Protocol — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The CKB hole punching protocol accepts a `from` field in `ConnectionRequest` messages that is fully attacker-controlled and is never verified against the actual session peer that sent the message. This allows any connected peer to impersonate any other peer ID, poisoning the `pending_delivered` state of a target node with attacker-chosen listen addresses, suppressing legitimate hole punching for victim peers, and bypassing the forward rate limiter.

---

### Finding Description

The `ConnectionRequest` molecule message carries a `from: Bytes` field representing the originating peer's ID. [1](#0-0) 

When a node receives this message, `ConnectionRequestProcess::execute()` parses `content.from` directly from the message bytes: [2](#0-1) 

The actual session that sent the message is available as `self.peer` (a `PeerIndex`): [3](#0-2) 

**At no point is `content.from` verified to match the peer identity of `self.peer`.** The code proceeds to use the unverified `content.from` in two security-sensitive ways:

**1. `pending_delivered` map poisoning.** When the receiving node is the intended `to` target (`self_peer_id == &content.to`), it calls `respond_delivered()`, which stores the attacker-supplied `from_peer_id` and attacker-supplied `listen_addrs` into the `pending_delivered` map: [4](#0-3) 

Later, when a `ConnectionSync` message arrives with `content.from` matching the poisoned key, `ConnectionSyncProcess::execute()` looks up `pending_delivered` and uses those stored addresses to initiate NAT traversal TCP connections: [5](#0-4) [6](#0-5) 

**2. Legitimate hole punching suppression.** Before inserting into `pending_delivered`, `respond_delivered()` checks whether a recent entry already exists for `from_peer_id`: [7](#0-6) 

If an attacker pre-populates `pending_delivered` with a victim's peer ID, the node will silently ignore all legitimate `ConnectionRequest` messages from that victim for up to `HOLE_PUNCHING_INTERVAL` (2 minutes): [8](#0-7) 

**3. Forward rate limiter bypass.** The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)`: [9](#0-8) 

Because `content.from` is attacker-controlled, an attacker can rotate fake `from` peer IDs to bypass the per-`(from, to)` rate limit and flood the network with forwarded messages.

The same unverified `from` pattern is present in `ConnectionRequestDelivered` and `ConnectionSync` handlers: [10](#0-9) [11](#0-10) 

---

### Impact Explanation

**NAT traversal hijacking:** An attacker who is connected to a victim node V can send a `ConnectionRequest` with `from = target_peer_id` (any arbitrary peer ID) and `listen_addrs = [attacker_ip:port]`. Node V stores this mapping. When a legitimate `ConnectionSync` later arrives for `target_peer_id`, V initiates NAT traversal TCP connections to the attacker's IP instead of the real peer's IP. This disrupts the hole punching connection establishment between V and the real target peer.

**Hole punching denial of service:** By pre-populating `pending_delivered` with a victim's peer ID, the attacker causes V to silently drop all legitimate `ConnectionRequest` messages from that victim for 2 minutes per poisoning event. This can be repeated continuously to permanently suppress hole punching for targeted peers.

**Rate limiter bypass:** Rotating fake `from` IDs allows an attacker to exceed the intended forwarding rate, amplifying message load on intermediate nodes.

---

### Likelihood Explanation

The attack requires only a standard P2P connection to the target node — no privileged access, no keys, no majority hash power. Any peer that can connect to the CKB P2P network can send a crafted `ConnectionRequest`. The `HolePunching` protocol is enabled by default and processes messages from all connected peers: [12](#0-11) 

The `from` field is a raw byte field with no cryptographic binding to the session, making spoofing trivial.

---

### Recommendation

In `ConnectionRequestProcess::execute()`, after parsing `content.from`, resolve the actual peer ID of `self.peer` from the peer registry and assert it equals `content.from`. If they do not match, reject the message (and optionally ban the peer):

```rust
// After parsing content.from:
let actual_peer_id = self.protocol.network_state
    .peer_registry.read()
    .get_peer(self.peer)
    .and_then(|p| p.identify_info.as_ref().map(|_| /* peer_id from session */));

if actual_peer_id.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId
        .with_context("from peer id does not match actual session sender");
}
```

Apply the same fix to `ConnectionRequestDeliveredProcess` and `ConnectionSyncProcess` for their respective `from` fields.

---

### Proof of Concept

1. Attacker peer A connects to victim node V.
2. A sends a `ConnectionRequest` message:
   - `from` = `victim_peer_id` (any arbitrary peer ID, not A's own)
   - `to` = V's own peer ID
   - `listen_addrs` = `[attacker_ip:attacker_port]`
   - `max_hops` = 1
3. V's `ConnectionRequestProcess::execute()` parses `content.from = victim_peer_id` without verifying it against A's session.
4. Since `content.to == self_peer_id`, V calls `respond_delivered(victim_peer_id, ...)`.
5. V stores `pending_delivered[victim_peer_id] = ([attacker_ip:attacker_port], now)`.
6. **Effect A (suppression):** Any legitimate `ConnectionRequest` from the real `victim_peer_id` arriving within 2 minutes is silently dropped at line 163–166 of `connection_request.rs`.
7. **Effect B (hijack):** When a `ConnectionSync` with `from = victim_peer_id` arrives at V, `ConnectionSyncProcess` looks up `pending_delivered[victim_peer_id]` and initiates NAT traversal TCP connections to `attacker_ip:attacker_port` instead of the real victim's addresses. [13](#0-12) [14](#0-13)

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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-115)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L118-128)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L24-24)
```rust
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L72-107)
```rust
    async fn received(&mut self, context: ProtocolContextMutRef<'_>, data: bytes::Bytes) {
        let session_id = context.session.id;
        trace!("HolePunching.received session={}", session_id);

        let msg = match packed::HolePunchingMessageReader::from_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                warn!(
                    "HolePunching.received a malformed message from {}",
                    session_id
                );
                self.network_state.ban_session(
                    &context.control().clone().into(),
                    session_id,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        let item_name = msg.item_name();

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
