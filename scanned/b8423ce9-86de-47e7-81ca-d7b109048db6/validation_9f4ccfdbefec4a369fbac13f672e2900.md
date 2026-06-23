### Title
Unauthenticated Route in `ConnectionRequestDelivered` Allows Targeted Peer Flooding via Any Relay — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

---

### Summary

A relay node processing a `ConnectionRequestDelivered` message unconditionally forwards it to `route.last()` without verifying that the route was established by a prior legitimate `ConnectionRequest`. An unprivileged attacker with one connection to a relay can direct that relay to flood any peer connected to it with HolePunching messages, at up to 30 messages/second per relay connection, with the per-(from,to) rate limiter trivially bypassed by varying the attacker-controlled `from`/`to` fields.

---

### Finding Description

The `execute()` method of `ConnectionRequestDeliveredProcess` handles the relay case at: [1](#0-0) 

```rust
match content.route.last() {
    Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
```

When `route` is non-empty, the relay immediately calls `forward_delivered(next_peer_id)` where `next_peer_id` is fully attacker-controlled. There is no check that the relay node ever forwarded a `ConnectionRequest` for this `(from, to)` pair, no check that `next_peer_id` was part of any prior legitimate route, and no check against `inflight_requests` or `pending_delivered`.

`forward_delivered` then resolves the attacker-supplied `PeerId` to a live session and sends the message: [2](#0-1) 

The two rate limiters present do **not** prevent this:

1. **Outer `rate_limiter`** (keyed by `(session_id, msg_item_id)`): 30 req/s per session per message type. This is the only real constraint — it caps the attacker at 30 forwarded messages/second per relay connection. [3](#0-2) 

2. **`forward_rate_limiter`** (keyed by `(from, to, msg_item_id)`): 1 req/s per `(from, to)` pair. This is trivially bypassed because `from` and `to` are attacker-controlled fields in the message body — the attacker simply varies them across requests. [4](#0-3) 

The `HolePunching` struct maintains `inflight_requests` and `pending_delivered` state, but these are only consulted when the local node is the terminal `from` peer (route is empty), never when acting as a relay. [5](#0-4) 

---

### Impact Explanation

An attacker with a single connection to relay R can:
- Craft `ConnectionRequestDelivered` messages with `route=[victim_peer_id]` and varying `from`/`to` values
- Cause R to forward up to 30 HolePunching messages/second to any peer connected to R
- With N relay connections, scale to 30N messages/second targeting one or many victims
- The victim must parse and process each message (including `DeliverdContent::try_from`, peer registry lookups, and rate-limiter key checks), consuming CPU and network bandwidth

The victim node will mostly return `StatusCode::Ignore` (no matching inflight request), but the processing overhead and network bandwidth consumption are real. At scale, this constitutes targeted network congestion of specific peers at very low attacker cost (one TCP connection per relay).

---

### Likelihood Explanation

The exploit requires only a standard P2P connection to any relay node running the HolePunching protocol. No special privileges, keys, or hashpower are needed. The attacker only needs to know the `PeerId` of the victim (which is public information on the CKB P2P network). The bypass of the `forward_rate_limiter` is trivial since `from` and `to` are free fields in the message.

---

### Recommendation

Before forwarding a `ConnectionRequestDelivered`, the relay should verify that it previously forwarded a `ConnectionRequest` for the same `(from, to)` pair. Concretely:

- Maintain a `forwarded_requests: HashMap<(PeerId, PeerId), u64>` (keyed by `(from, to)`, value = timestamp) that is populated in `ConnectionRequestProcess::forward_message`.
- In `ConnectionRequestDeliveredProcess::execute`, when `route.last()` is `Some`, check that `forwarded_requests` contains a recent entry for `(content.from, content.to)` before calling `forward_delivered`.
- Remove the entry after forwarding the delivered message (or after timeout) to prevent replay.

This enforces the invariant that `ConnectionRequestDelivered` can only travel back along a route that was established by a legitimate `ConnectionRequest`.

---

### Proof of Concept

```
1. Attacker A connects to relay R via standard P2P.
2. A observes (or discovers via normal peer exchange) that victim V is connected to R.
3. A crafts a series of ConnectionRequestDelivered messages:
     - route = [V.peer_id]
     - from  = random_peer_id_i   (different each time, bypasses forward_rate_limiter)
     - to    = random_peer_id_i'
     - listen_addrs = [any valid TCP multiaddr with to's peer_id]
     - sync_route = []
4. A sends these at ~30/s to R (capped by outer rate_limiter).
5. R executes ConnectionRequestDeliveredProcess::execute():
     - route.last() = V.peer_id  → calls forward_delivered(V.peer_id)
     - peer_registry.get_key_by_peer_id(V.peer_id) → returns V's session_id
     - send_message_to(V.session_id, HolePunching, message) → message delivered to V
6. V receives 30 HolePunching messages/second from R, each requiring parse + processing.
7. Assert: V receives messages without R ever having forwarded a ConnectionRequest for any of these (from, to) pairs.
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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L147-148)
```rust
        match content.route.last() {
            Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
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

**File:** network/src/protocols/hole_punching/mod.rs (L42-46)
```rust
    inflight_requests: HashMap<PeerId, u64>,
    // Delivered timestamp recorded
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
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
