### Title
Attacker-Controlled `from` Field in `ConnectionRequest` Enables Peer Identity Spoofing in Hole-Punching Protocol — (`network/src/protocols/hole_punching/component/connection_request.rs`)

### Summary

The hole-punching protocol's `ConnectionRequest` message contains a `from` field (the initiating peer's ID) that is taken directly from the attacker-controlled message payload. The handler validates that `from` is a well-formed `PeerId` and that embedded peer IDs in `listen_addrs` match `from`, but it never verifies that `from` matches the actual session's authenticated peer ID. Any connected peer can therefore impersonate an arbitrary victim peer in hole-punching requests, causing relay nodes to forward spoofed requests and target peers to store and act on attacker-supplied addresses attributed to the victim.

### Finding Description

**Root cause — `from` is never checked against the session peer ID.**

In `connection_request.rs`, the `TryFrom` implementation parses `from` out of the raw message bytes:

```rust
let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
    StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
})?;
```

The only validation performed is that `from` is a syntactically valid `PeerId` and that any peer ID embedded in `listen_addrs` matches `from`. There is no check that `from` equals the peer ID of the actual authenticated TLS/Noise session that delivered the message. The session's real peer ID is available in the `ProtocolContextMutRef` (via `context.session.id` → registry lookup), but it is never compared to `from`.

**Exploit flow:**

1. Attacker (peer A, connected to relay node R) crafts a `ConnectionRequest` with `from = victim_peer_id` (peer B's ID) and `listen_addrs = [attacker_controlled_addr]` (with peer B's ID appended to satisfy the addr-consistency check).
2. Relay node R parses the message, passes all validation, and calls `forward_message` toward the target peer C (or broadcasts if C is not directly connected).
3. Target peer C receives a hole-punching request that appears to originate from peer B, with `listen_addrs` pointing to the attacker's infrastructure.
4. C calls `respond_delivered`, stores `(attacker_addr, timestamp)` in `pending_delivered` keyed by `victim_peer_id`, and sends a `ConnectionRequestDelivered` back through the route.
5. When a subsequent `ConnectionSync` arrives, C attempts to connect to the attacker's address, believing it is connecting to peer B.

**Secondary DoS path:** The attacker can flood `pending_delivered` with arbitrary `from` peer IDs. Because `respond_delivered` rate-limits by `from_peer_id`, pre-inserting an entry for the victim's peer ID blocks any legitimate hole-punching request from the real victim for the duration of `HOLE_PUNCHING_INTERVAL`.

**Schema reference** — the `from` field is defined as an unverified byte blob:

```
table ConnectionRequest {
    from: Bytes,   // attacker-controlled
    to: Bytes,
    ...
    listen_addrs: AddressVec,
}
```

### Impact Explanation

- **Connection hijacking**: Target peers are directed to connect to attacker-controlled addresses under the identity of an arbitrary victim peer. This enables man-in-the-middle positioning for subsequent P2P sessions.
- **Peer identity spoofing**: Any peer in the network can impersonate any other peer in hole-punching signaling, breaking the trust model of the NAT-traversal protocol.
- **Targeted DoS**: An attacker can pre-poison `pending_delivered` for a victim's peer ID, silently dropping all legitimate hole-punching requests from that victim for the rate-limit window.

### Likelihood Explanation

The attacker only needs to be a connected peer — no privileged access, no key material, no majority hash power. The `ConnectionRequest` message is a standard P2P protocol message any peer can send. The spoofed `from` field passes all existing validation because the checks are purely structural (valid PeerId bytes, addr consistency), not cryptographic. This is reachable by any unprivileged network peer.

### Recommendation

After parsing `from` from the message, verify it against the authenticated session peer ID:

```rust
// Retrieve the actual peer ID from the session context
let actual_peer_id = extract_peer_id(&context.session.address)
    .ok_or_else(|| StatusCode::InvalidFromPeerId.with_context("session has no peer id"))?;

if content.from != actual_peer_id {
    return StatusCode::InvalidFromPeerId
        .with_context("from field does not match authenticated session peer id")
        .into();
}
```

This mirrors the correct pattern used elsewhere in the codebase (e.g., `identify/mod.rs` uses `context.session.id` to look up the peer and update its state, never trusting peer-supplied identity fields for authorization).

### Proof of Concept

1. Connect peer A to a CKB node R that has peer B and peer C also connected (or reachable).
2. Peer A sends a `ConnectionRequest` molecule-encoded message on the `HolePunching` protocol with:
   - `from` = peer B's known `PeerId` bytes
   - `to` = peer C's `PeerId` bytes
   - `listen_addrs` = one address controlled by the attacker, with peer B's peer ID appended as the `/p2p/` component
   - `max_hops` = 3, `route` = []
3. Node R parses the message: `from` is a valid PeerId ✓, addr peer ID matches `from` ✓ — no session-identity check is performed.
4. R forwards the request toward C.
5. C receives a hole-punching request attributed to peer B with the attacker's address as B's listen address.
6. C stores the attacker's address in `pending_delivered[victim_peer_id]` and sends `ConnectionRequestDelivered` back.
7. On `ConnectionSync`, C dials the attacker's address, completing the hijack. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L32-61)
```rust
impl TryFrom<&packed::ConnectionRequestReader<'_>> for RequestContent {
    type Error = Status;

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L155-238)
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L242-307)
```rust
    async fn forward_message(&self, self_peer_id: &PeerId, to_peer_id: &PeerId) -> Status {
        let content = forward_request(self.message, self_peer_id);
        let new_message = packed::HolePunchingMessage::new_builder()
            .set(content)
            .build()
            .as_bytes();
        let proto_id = SupportProtocols::HolePunching.protocol_id();

        let target_sid = self
            .protocol
            .network_state
            .peer_registry
            .read()
            .get_key_by_peer_id(to_peer_id);

        match target_sid {
            Some(to_peer) => {
                debug!(
                    "target peer {} is found, forward the request to it",
                    to_peer_id
                );
                if let Err(error) = self
                    .p2p_control
                    .send_message_to(to_peer, proto_id, new_message)
                    .await
                {
                    StatusCode::ForwardError.with_context(error)
                } else {
                    Status::ok()
                }
            }
            None => {
                debug!(
                    "target peer {} is not found, broadcast the request to more peers",
                    to_peer_id
                );

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
        }
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
