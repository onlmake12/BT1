### Title
Unauthenticated `ConnectionRequestDelivered` Relay Forwarding Allows Relay Abuse and Targeted NAT-State Poisoning — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

---

### Summary

`ConnectionRequestDeliveredProcess::execute` forwards a `ConnectionRequestDelivered` message to the peer identified by `content.from` when `route` is empty and the relay is not `content.from`. There is no check that the sender of the message is actually the legitimate `to` peer from the original `ConnectionRequest`. Any unprivileged connected peer can craft a message with an empty route and an arbitrary `from` field, causing the relay to deliver attacker-controlled content to any peer whose `PeerId` the attacker knows and who is currently connected to the relay.

---

### Finding Description

The critical branch is in `execute()`: [1](#0-0) 

When `route` is empty, the relay checks only whether its own `self_peer_id` equals `content.from`. If not, it unconditionally calls `forward_delivered(&content.from)`: [2](#0-1) 

`forward_delivered` looks up the session for `content.from` in the peer registry and sends the message verbatim (with attacker-controlled `listen_addrs`, `to`, `sync_route`) to that session. There is no check that:
- The sender (`self.peer`) is the peer identified by `content.to` in the original request.
- A prior `ConnectionRequest` from `content.from` to `content.to` was ever seen by this relay.
- The `listen_addrs` in the message were produced by the legitimate `to` peer.

The `forward_delivered` helper preserves all attacker-supplied fields when route is already empty: [3](#0-2) 

---

### Impact Explanation

**Relay abuse (unconditional):** Any peer connected to a relay can cause that relay to send a `ConnectionRequestDelivered` message to any other peer also connected to the relay, simply by setting `from=target_peer_id` and `route=[]`. The attacker pays one message; the relay performs a registry lookup and sends one message to the target.

**NAT-state poisoning (conditional):** When the target receives the forwarded message, it also has `route.last() == None` and `self_peer_id == content.from`, so it enters the terminal branch: [4](#0-3) 

If `content.to` matches an active entry in `inflight_requests`, the target calls `try_nat_traversal` with the attacker-supplied `listen_addrs`. This causes the victim to spend up to 30 seconds repeatedly attempting TCP connections to attacker-controlled addresses, consuming file descriptors and CPU, and potentially leaking the victim's source port to the attacker's server.

**Rate-limiter bypass:** The forward rate limiter is keyed on `(from, to, msg_item_id)`: [5](#0-4) 

By varying `content.to` across messages (all valid `PeerId` bytes), the attacker can send up to the outer per-session cap of 30 messages/second, each targeting a different `(from, to)` pair, bypassing the forward rate limiter entirely. With multiple connections the cap scales linearly.

---

### Likelihood Explanation

- The attacker needs only a standard P2P connection to any relay node — no special privileges.
- `PeerId` values are public (advertised via the identify protocol and peer store).
- The relay does not need to have seen any prior `ConnectionRequest` for the `(from, to)` pair.
- The NAT-poisoning branch additionally requires the attacker to know an active `inflight_requests` entry on the target, which is harder but not impossible (the target broadcasts `ConnectionRequest` via gossip, so the attacker can observe `from`/`to` pairs in flight).

---

### Recommendation

1. **Verify sender identity at the relay:** Before forwarding, check that `self.peer` (the session that sent this message) corresponds to the peer identified by `content.to`. If the relay has no record of a `ConnectionRequest` it forwarded for this `(from, to)` pair, drop the message.
2. **Track forwarded requests:** Maintain a relay-side map of `(from, to)` pairs for which a `ConnectionRequest` was forwarded, and only forward `ConnectionRequestDelivered` for known pairs.
3. **Bind the forward rate limiter to the sending session:** Key the forward rate limiter on `(session_id, from, to)` rather than just `(from, to, item_id)` to prevent per-session bypass via `to` variation.

---

### Proof of Concept

```
1. Attacker A connects to relay R (standard P2P handshake).
2. Victim V is also connected to R; A learns V's PeerId via identify/peer-store gossip.
3. A crafts:
     ConnectionRequestDelivered {
       from: V.peer_id,          // arbitrary — set to victim
       to:   <any valid PeerId>, // e.g. A's own peer_id
       route: [],                // empty — triggers the None branch
       sync_route: [],
       listen_addrs: [attacker_controlled_addr/p2p/V.peer_id],
     }
4. A sends this to R over the HolePunching protocol.
5. R executes: route.last() == None, self_peer_id != V.peer_id → forward_delivered(V.peer_id).
6. R looks up V's session in peer_registry and sends the message to V.
7. V receives it: route.last() == None, self_peer_id == content.from → terminal branch.
8. If V has inflight_requests[content.to], V calls try_nat_traversal(ttl, [attacker_controlled_addr]).
9. V spends up to 30 s making TCP SYN packets to attacker_controlled_addr, leaking source port.
```

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L147-153)
```rust
        match content.route.last() {
            Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
            None => {
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.from {
                    // forward the message to the `from` peer
                    self.forward_delivered(&content.from).await
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L160-176)
```rust
                    let request_start = self.protocol.inflight_requests.remove(&content.to);

                    match request_start {
                        Some(start) => {
                            let res = self.respond_sync(content.from).await;
                            if !res.is_ok() {
                                return res;
                            }
                            let now = unix_time_as_millis();
                            let ttl = now - start;

                            self.try_nat_traversal(ttl, content.listen_addrs);

                            Status::ok()
                        }
                        None => StatusCode::Ignore.with_context("the request is not in flight"),
                    }
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L182-213)
```rust
    async fn forward_delivered(&self, peer_id: &PeerId) -> Status {
        let target_sid = self
            .protocol
            .network_state
            .peer_registry
            .read()
            .get_key_by_peer_id(peer_id);
        match target_sid {
            Some(next_peer) => {
                let content = forward_delivered(self.message);
                let new_message = packed::HolePunchingMessage::new_builder()
                    .set(content)
                    .build()
                    .as_bytes();
                let proto_id = SupportProtocols::HolePunching.protocol_id();
                debug!(
                    "forward the delivery to next peer {} (id: {})",
                    next_peer, peer_id
                );
                if let Err(error) = self
                    .p2p_control
                    .send_message_to(next_peer, proto_id, new_message)
                    .await
                {
                    StatusCode::ForwardError.with_context(error)
                } else {
                    Status::ok()
                }
            }
            None => StatusCode::Ignore.with_context("the next peer in the route is disconnected"),
        }
    }
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L228-240)
```rust
    let new_route = if route.is_empty() {
        packed::BytesVec::new_builder().build()
    } else {
        packed::BytesVec::new_builder()
            .extend(
                message
                    .route()
                    .into_iter()
                    .take(route.len().saturating_sub(1)),
            )
            .build()
    };
    message.as_builder().route(new_route).build()
```
