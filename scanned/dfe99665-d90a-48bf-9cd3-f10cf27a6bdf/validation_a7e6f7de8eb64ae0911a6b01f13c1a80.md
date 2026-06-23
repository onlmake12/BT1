### Title
Unauthenticated `ConnectionRequestDelivered` Forwarding Allows Relay-Amplified Message Flooding — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

---

### Summary

A relay node unconditionally forwards a `ConnectionRequestDelivered` message to whichever peer appears in the attacker-controlled `route` field, with no check that the route was established by a prior legitimate `ConnectionRequest`. Any unprivileged peer connected to a relay can exploit this to direct the relay to send HolePunching messages to any other peer connected to that relay.

---

### Finding Description

In `execute()`, after basic structural validation, the node branches on `content.route.last()`: [1](#0-0) 

If the route is non-empty, it immediately calls `forward_delivered(next_peer_id)` with the attacker-supplied peer ID: [2](#0-1) 

`forward_delivered` resolves the peer ID against the live peer registry and calls `send_message_to` — no state is consulted to verify that this `(from, to, route)` tuple was ever produced by a real `ConnectionRequest` traversal. The `pending_delivered` map (which tracks legitimate in-flight requests) is only consulted in the `route.is_empty()` branch (the terminal case for the `from` peer), never in the forwarding branch: [3](#0-2) 

By contrast, the `ConnectionRequest` handler does record state (`inflight_requests`) and the `respond_delivered` path checks `pending_delivered` for replay suppression, but none of this state is checked when forwarding a `ConnectionRequestDelivered`: [4](#0-3) 

---

### Impact Explanation

An attacker with one connection to relay R can:

1. Craft a `ConnectionRequestDelivered` with arbitrary `from`, `to`, and `route=[victim_peer_id]` where `victim_peer_id` is any peer connected to R.
2. R calls `forward_delivered(victim_peer_id)`, finds the victim in its registry, and sends the message to the victim.
3. By varying `from`/`to` fields, the attacker bypasses the `forward_rate_limiter` (keyed on `(from, to, msg_item_id)`): [5](#0-4) 

The outer per-session rate limiter caps at 30 msg/s per session: [6](#0-5) 

So each attacker connection delivers up to 30 `ConnectionRequestDelivered` messages per second to the victim, amplified through the relay. With multiple connections or multiple relays, this scales linearly.

---

### Likelihood Explanation

The path requires only a standard P2P connection to any relay node — no privileges, no keys, no PoW. The message is structurally valid (just needs valid PeerIds and 1–24 listen addresses). The exploit is locally testable and requires no network-wide coordination.

---

### Recommendation

Before forwarding a `ConnectionRequestDelivered`, the relay should verify that it has a corresponding entry in its own forwarding state — i.e., that it previously forwarded a `ConnectionRequest` for the same `(from, to)` pair and that the claimed `route` is consistent with what it recorded. If no such state exists, the message should be dropped (and optionally the sender penalized). This is analogous to how `inflight_requests` is used on the terminal peer: [7](#0-6) 

A similar guard map (e.g., `inflight_forwards: HashMap<(PeerId, PeerId), u64>`) should be populated when a `ConnectionRequest` is forwarded and checked/consumed when the corresponding `ConnectionRequestDelivered` arrives.

---

### Proof of Concept

```
1. Attacker A connects to relay R (one standard P2P connection).
2. Victim V is also connected to R.
3. A sends R a ConnectionRequestDelivered:
     from    = <any valid PeerId, e.g. random>
     to      = <any valid PeerId, e.g. random>
     route   = [V.peer_id]          ← attacker-controlled
     listen_addrs = [<any valid TCP multiaddr>]
     sync_route   = []
4. R.execute() sees route.last() = V.peer_id, calls forward_delivered(V.peer_id).
5. R.peer_registry.get_key_by_peer_id(V.peer_id) returns V's session ID.
6. R sends the ConnectionRequestDelivered message to V.
7. Repeat with different (from, to) pairs to bypass forward_rate_limiter,
   up to 30 times/second per attacker session.
```

No prior `ConnectionRequest` is needed. No state on R is checked. The relay acts as an unconditional message proxy to any of its connected peers.

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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L147-179)
```rust
        match content.route.last() {
            Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
            None => {
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
                }
            }
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
