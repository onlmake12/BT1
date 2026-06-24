Audit Report

## Title
Unauthenticated `ConnectionRequestDelivered` Relay with Bypassed `forward_rate_limiter` Enables Targeted Peer Flooding — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary
`ConnectionRequestDeliveredProcess::execute()` unconditionally forwards a `ConnectionRequestDelivered` message to the attacker-supplied `route.last()` peer with no verification that the relay ever processed a corresponding `ConnectionRequest` for the same `(from, to)` pair. The `forward_rate_limiter` intended to throttle this path is completely ineffective because its key `(from, to, msg_item_id)` is fully attacker-controlled via the message body. The only real constraint is the outer `rate_limiter` at 30 req/s per session, allowing an attacker with a single relay connection to direct that relay to flood any connected peer with 30 HolePunching messages/second.

## Finding Description
In `execute()`, when `content.route.last()` is `Some`, the code immediately calls `self.forward_delivered(next_peer_id).await` with no state validation:

```rust
match content.route.last() {
    Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
``` [1](#0-0) 

`forward_delivered` resolves the attacker-supplied `PeerId` via `peer_registry.get_key_by_peer_id` and calls `send_message_to` on the resulting session: [2](#0-1) 

There is no check that the relay ever forwarded a `ConnectionRequest` for this `(from, to)` pair. `inflight_requests` is only populated in `notify()` when the node itself initiates a request: [3](#0-2) 

And `pending_delivered` is only populated in `ConnectionRequestProcess::respond_delivered` (the terminal `to`-peer case), never during relay forwarding: [4](#0-3) 

`ConnectionRequestProcess::forward_message` records no shared state that `ConnectionRequestDeliveredProcess` could consult: [5](#0-4) 

The two rate limiters fail to prevent this:

1. **`forward_rate_limiter`** (keyed by `(from, to, msg_item_id)`, 1 req/s): The key is `(content.from, content.to, self.msg_item_id)` where `from` and `to` come directly from the attacker-controlled message body, and `msg_item_id` is a constant message-type identifier. The attacker trivially bypasses this by using a different `(from, to)` pair per message: [6](#0-5) 

2. **`rate_limiter`** (keyed by `(session_id, msg_item_id)`, 30 req/s): This is the only real constraint. It caps the attacker at 30 forwarded messages/second per relay connection but does not prevent the attack: [7](#0-6) 

When the victim V receives the forwarded message (with `route = []` after the relay pops the last hop via `forward_delivered`), V's `execute()` enters the `None` branch, finds `self_peer_id != content.from` (since `from` is a random peer ID), and calls `forward_delivered(&content.from)`, which performs a peer registry lookup and returns `StatusCode::Ignore`. This forces V to execute message parsing, `forward_rate_limiter` key check, and peer registry lookup for every message. [8](#0-7) 

## Impact Explanation
This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker with a single TCP connection to relay R can direct R to send 30 HolePunching messages/second to any peer V connected to R. With connections to N relays, the attacker scales to 30N messages/second targeting V. The `forward_rate_limiter` — the mechanism specifically designed to prevent this — is completely ineffective because its key is attacker-controlled. This is a clear bad design enabling targeted per-peer flooding at minimal attacker cost.

## Likelihood Explanation
The exploit requires only a standard P2P connection to any relay node running the HolePunching protocol — no special privileges, keys, or hashpower. The victim's `PeerId` is public information on the CKB P2P network. Bypassing the `forward_rate_limiter` requires only varying the `from`/`to` fields across messages, which is trivial. The attack is repeatable and persistent as long as the attacker maintains the relay connection.

## Recommendation
Before forwarding a `ConnectionRequestDelivered`, the relay should verify it previously forwarded a `ConnectionRequest` for the same `(from, to)` pair:

1. Add a `forwarded_requests: HashMap<(PeerId, PeerId), u64>` field to `HolePunching`, populated in `ConnectionRequestProcess::forward_message` with the current timestamp.
2. In `ConnectionRequestDeliveredProcess::execute`, when `route.last()` is `Some`, check that `forwarded_requests` contains a recent entry for `(content.from, content.to)` before calling `forward_delivered`.
3. Remove the entry after forwarding (or after a timeout matching `TIMEOUT`) to prevent replay.
4. Optionally, verify that `next_peer_id` matches the peer that originally sent the `ConnectionRequest`.

## Proof of Concept
```
1. Attacker A establishes a standard P2P connection to relay R.
2. A observes (via peer exchange or network scanning) that victim V is connected to R.
3. A crafts ConnectionRequestDelivered messages in a loop:
     - route        = [V.peer_id]         (attacker-controlled, directs relay to V)
     - from         = random_peer_id_i    (different each iteration, bypasses forward_rate_limiter)
     - to           = random_peer_id_i'   (different each iteration)
     - listen_addrs = [valid TCP multiaddr embedding to's peer_id]
     - sync_route   = []
4. A sends ~30 such messages/second to R (bounded by outer rate_limiter at 30/s per session).
5. For each message, R executes ConnectionRequestDeliveredProcess::execute():
     a. forward_rate_limiter check passes (new (from, to) key each time)
     b. route.last() = V.peer_id → forward_delivered(V.peer_id)
     c. peer_registry.get_key_by_peer_id(V.peer_id) → V's session_id
     d. send_message_to(V.session_id, HolePunching, message) → delivered to V
6. V receives 30 HolePunching messages/second from R, each requiring parse + processing.
7. Invariant violated: R forwarded ConnectionRequestDelivered for (from_i, to_i) pairs
   for which it never forwarded any ConnectionRequest.
8. With N relay connections, attacker scales to 30N messages/second targeting V.
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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L149-153)
```rust
            None => {
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.from {
                    // forward the message to the `from` peer
                    self.forward_delivered(&content.from).await
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

**File:** network/src/protocols/hole_punching/mod.rs (L239-242)
```rust
            let now = unix_time_as_millis();
            for peer_id in inflight {
                self.inflight_requests.insert(peer_id, now);
            }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
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
