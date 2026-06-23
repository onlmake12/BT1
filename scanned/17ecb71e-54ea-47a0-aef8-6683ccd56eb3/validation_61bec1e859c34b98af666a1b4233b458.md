The vulnerability is real. Let me trace the exact code path.

**Entry point**: Any peer connected to a relay node can send a `ConnectionRequestDelivered` P2P message with an arbitrary `route` field.

**`execute()` flow** in `connection_request_delivered.rs`:

1. The message is parsed — `route` is accepted as-is from the wire with no cross-reference to any prior `ConnectionRequest` state. [1](#0-0) 

2. The only guards applied are: route length ≤ `MAX_HOPS` (6), `listen_addrs` count 1–24, and a `forward_rate_limiter` keyed by `(content.from, content.to, msg_item_id)` — all three values are attacker-controlled fields in the message. [2](#0-1) 

3. If `route` is non-empty, `content.route.last()` is used directly as the forwarding target — no check that this peer was ever part of an original `ConnectionRequest` route. [3](#0-2) 

4. `forward_delivered()` looks up the attacker-supplied `peer_id` in the live peer registry and unconditionally sends the message to it. [4](#0-3) 

There is no state stored from `ConnectionRequest` processing that is consulted here. The `inflight_requests` map (keyed by `to` peer) is only checked in the terminal case (`route` is empty and `self == from`), never during forwarding. [5](#0-4) 

**Rate limiter bypass**: The session-level limiter is 30 req/sec per session [6](#0-5)  and the forward limiter is 1 req/sec per `(from, to)` pair [7](#0-6)  — but since `from` and `to` are attacker-controlled message fields, the attacker can rotate them freely to bypass the forward limiter, and open multiple sessions to multiply the session-level cap.

---

### Title
Unauthenticated Arbitrary Peer Forwarding via Attacker-Controlled `route` in `ConnectionRequestDelivered` — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

### Summary
`ConnectionRequestDeliveredProcess::execute()` forwards the message to whatever `PeerId` appears last in the attacker-supplied `route` field, with no verification that this route was established by a prior `ConnectionRequest`. Any connected peer can use a relay node as an unconditional message forwarder to any other peer connected to that relay.

### Finding Description
When a relay node receives a `ConnectionRequestDelivered` message, `execute()` calls `content.route.last()` and passes the result directly to `forward_delivered()`. That function looks up the peer in the live registry and sends the message. The relay never checks whether the `route` field matches any state recorded during a `ConnectionRequest`. The `inflight_requests` map is only consulted in the terminal delivery branch (empty route, self == from), not during forwarding. The `forward_rate_limiter` key `(content.from, content.to, msg_item_id)` is entirely attacker-controlled, making it trivially bypassable by rotating `from`/`to` values.

### Impact Explanation
An attacker connected to relay R can target any peer P also connected to R by crafting `ConnectionRequestDelivered { route: [P], from: <random>, to: <random>, listen_addrs: [valid_addr] }`. R forwards the message to P. With multiple sessions (each allowing 30 req/sec) and rotated `from`/`to` pairs, the attacker can flood P with hole-punching messages at high rate, causing CPU/memory pressure from message processing and NAT traversal attempts, and can selectively target specific peers for harassment.

### Likelihood Explanation
Requires only a standard P2P connection to the relay — no privilege, no key, no PoW. The target peer's `PeerId` is discoverable via the discovery protocol. The attack is fully local-testable and requires no coordination.

### Recommendation
Before forwarding a `ConnectionRequestDelivered`, the relay must verify that the `route` field's last entry matches the peer that sent the message (i.e., the sender's `PeerId` must equal `route.last()`), and that the `(from, to)` pair corresponds to a `ConnectionRequest` that was previously forwarded by this node. A per-relay forwarding state table (analogous to `inflight_requests`) should be maintained to enforce this.

### Proof of Concept
1. Attacker A connects to relay R via the HolePunching protocol.
2. A discovers that peer P (PeerId = `P_id`) is connected to R.
3. A sends: `ConnectionRequestDelivered { from: random_id_1, to: random_id_2, route: [P_id], sync_route: [], listen_addrs: [valid_multiaddr] }`
4. R's `execute()` hits `content.route.last() == Some(P_id)`, calls `forward_delivered(P_id)`.
5. R looks up `P_id` in `peer_registry`, finds the session, and sends the message to P.
6. P receives the unsolicited `ConnectionRequestDelivered` and processes it.
7. Repeat with rotated `from`/`to` to bypass the forward rate limiter; open additional sessions to multiply throughput.

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L43-50)
```rust
        let route = value
            .route()
            .iter()
            .map(|peer_id| {
                PeerId::from_bytes(peer_id.raw_data().to_vec())
                    .map_err(|_| StatusCode::InvalidRoute)
            })
            .collect::<Result<Vec<_>, _>>()?;
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L130-145)
```rust
        if content.route.len() > MAX_HOPS as usize || content.sync_route.len() > MAX_HOPS as usize {
            return StatusCode::InvalidRoute.with_context("the route length is too long");
        }

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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L147-148)
```rust
        match content.route.last() {
            Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L182-212)
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
