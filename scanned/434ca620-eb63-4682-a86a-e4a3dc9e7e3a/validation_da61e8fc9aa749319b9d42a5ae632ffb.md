### Title
Unauthenticated `from` Peer ID in `ConnectionRequest` Allows Any Peer to Poison `pending_delivered` Map and Bypass Rate Limits - (File: network/src/protocols/hole_punching/component/connection_request.rs)

### Summary
The hole-punching protocol's `ConnectionRequest` handler extracts the `from` peer ID from the message payload without verifying it matches the actual peer ID of the connected session. Any peer can forge a `ConnectionRequest` claiming to originate from any arbitrary peer ID, enabling DoS via `pending_delivered` map poisoning and rate-limiter bypass.

### Finding Description

The `ConnectionRequest` message schema contains a `from: Bytes` field (peer ID) embedded in the message payload: [1](#0-0) 

In `RequestContent::try_from`, the `from` peer ID is decoded exclusively from the message content: [2](#0-1) 

The actual network session that delivered the message is available as `self.peer` (a `PeerIndex`) in `ConnectionRequestProcess`, but **the handler never verifies that `content.from` matches the peer ID of the actual connected session**: [3](#0-2) 

This is the direct analog of the reported Solidity bug: the `sender` (here `content.from`) is taken from attacker-controlled data rather than from the authenticated transport context.

The spoofed `from` is then used in two security-relevant ways:

**1. `pending_delivered` map poisoning:**
When the receiving node is the intended `to` target, `respond_delivered` is called. It checks `pending_delivered` keyed by `from_peer_id` to enforce a 2-minute cooldown, then inserts the spoofed `from_peer_id` into the map: [4](#0-3) [5](#0-4) 

An attacker can pre-populate `pending_delivered` with any legitimate peer's ID and a current timestamp. When the legitimate peer later sends a real `ConnectionRequest`, the target node will find the poisoned entry and silently ignore the request for the full `HOLE_PUNCHING_INTERVAL` (2 minutes): [6](#0-5) 

**2. Forward rate-limiter bypass:**
The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)`: [7](#0-6) 

By cycling through different spoofed `from` values, an attacker can bypass the per-pair rate limit entirely and flood intermediate nodes with forwarded `ConnectionRequest` messages, amplifying network traffic across the gossip broadcast path: [8](#0-7) 

### Impact Explanation

- **DoS against hole-punching**: An attacker connected to any node that is also connected to a victim target can block any specific peer from successfully completing hole-punching through that target for repeated 2-minute windows by continuously refreshing the poisoned `pending_delivered` entry.
- **Rate-limit bypass and network amplification**: By spoofing `from`, an attacker bypasses the `forward_rate_limiter` and causes intermediate nodes to gossip-broadcast forged requests to `sqrt(total_peers)` neighbors, amplifying traffic across the P2P network.

### Likelihood Explanation

Any unprivileged peer connected to the network can send a `ConnectionRequest` with an arbitrary `from` field. No special privileges, keys, or majority hashpower are required. The hole-punching protocol is reachable from any inbound or outbound P2P connection.

### Recommendation

After parsing `content.from`, look up the actual peer ID of the session from the peer registry using `self.peer` (the `PeerIndex`) and reject the message if `content.from` does not match:

```rust
// After parsing content.from, verify it matches the actual session peer
let actual_peer_id = self.protocol.network_state
    .with_peer_registry(|reg| reg.get_peer(self.peer).map(|p| p.peer_id.clone()));
if actual_peer_id.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId
        .with_context("from peer id does not match actual session peer");
}
```

### Proof of Concept

1. Attacker peer `A` connects to node `N`, which is also connected to target node `T`.
2. `A` sends a `ConnectionRequest` to `N` with `from = <legitimate_peer_B_id>` and `to = <T_peer_id>`.
3. `N` forwards the request to `T` (or gossip-broadcasts it).
4. `T` receives the request, sees `self_peer_id == content.to`, calls `respond_delivered`, and inserts `<legitimate_peer_B_id>` into `pending_delivered` with the current timestamp.
5. When legitimate peer `B` sends a real `ConnectionRequest` to `T` within the next 2 minutes, `T` finds the poisoned entry and returns `StatusCode::Ignore`, silently dropping the legitimate hole-punching attempt.
6. `A` repeats step 2 every ~2 minutes to maintain the block indefinitely. [9](#0-8)

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L35-38)
```rust
    fn try_from(value: &packed::ConnectionRequestReader<'_>) -> Result<Self, Self::Error> {
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L85-153)
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L155-167)
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
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-238)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L24-24)
```rust
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
```
