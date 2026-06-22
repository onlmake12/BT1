### Title
Unauthenticated `from` Field in `ConnectionRequestDelivered` Enables Relay-Assisted Message Injection and Forced NAT Traversal to Attacker-Controlled Addresses — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

---

### Summary

Any peer connected to a relay node can forge the `from` field of a `ConnectionRequestDelivered` message and set `route = []`. The relay's `execute()` handler, finding an empty route and `self_peer_id != content.from`, unconditionally calls `forward_delivered(&content.from)`, which looks up the forged peer ID in the live peer registry and delivers the attacker-crafted message to that peer. No check ever verifies that `content.from` matches the actual sender's authenticated peer ID.

---

### Finding Description

The `execute()` method in `ConnectionRequestDeliveredProcess` dispatches on `content.route.last()`: [1](#0-0) 

When `route` is empty and `self_peer_id != &content.from`, the relay calls:

```rust
self.forward_delivered(&content.from).await
```

`forward_delivered` (the method) resolves `content.from` against the live peer registry and sends the message to whatever session ID maps to that peer ID: [2](#0-1) 

The helper `forward_delivered` (the free function) passes the message through verbatim, only rebuilding the (already-empty) route field: [3](#0-2) 

The `DeliverdContent::try_from` parser validates only that `from` bytes decode to a syntactically valid `PeerId`; it never checks that `from` equals the authenticated peer ID of the TCP session that delivered the message: [4](#0-3) 

The rate limiter is keyed by `(content.from, content.to, msg_item_id)`: [5](#0-4) 

An attacker trivially bypasses it by varying `content.to` or `msg_item_id` across requests.

---

### Impact Explanation

**Stage 1 — Relay-assisted message injection.** Attacker A, connected to relay B, sends a `ConnectionRequestDelivered` with `from = C_peer_id`, `route = []`, `listen_addrs = [attacker_IP:port]`, `to = <any peer_id>`. B forwards the fully attacker-controlled message to C (if C is connected to B). The forwarded message is indistinguishable from a legitimate protocol message.

**Stage 2 — Forced outbound TCP connections on C (SSRF-like).** When C receives the injected message, it enters the terminal branch: [6](#0-5) 

If `content.to` matches any entry in C's `inflight_requests` map, C calls `try_nat_traversal(ttl, content.listen_addrs)`, which spawns a background task making repeated outbound TCP connection attempts to the attacker-supplied addresses for up to 30 seconds. The attacker controls the target IP and port entirely.

**Stage 3 — `ConnectionSync` injection.** The same branch calls `respond_sync(content.from)`, sending a `ConnectionSync` back toward B with attacker-controlled `from`, `to`, and `sync_route` fields, further polluting the hole-punching state machine of any node along the forged sync route.

---

### Likelihood Explanation

- Requires only a standard P2P connection to any relay node — no privileges, no keys, no majority hashpower.
- The `inflight_requests` precondition for Stage 2 is observable: `ConnectionRequest` broadcasts are sent to `sqrt(total_peers)` nodes, so an attacker connected to B can observe C's broadcasts and learn which `to` peer IDs C is currently pursuing.
- Alternatively, the attacker can first trigger C to initiate hole-punching (by manipulating peer store entries) and then immediately send the forged `ConnectionRequestDelivered`.
- Rate limiting is trivially bypassed by varying `msg_item_id` or `content.to`.

---

### Recommendation

1. **Authenticate `from` against the session.** At the start of `execute()`, verify that `content.from` equals the peer ID of the session that delivered the message (`self.peer`). The peer ID is already available via the peer registry keyed by `self.peer` (session ID). Reject with a ban-worthy status code if they differ.

2. **Authenticate `from` at relay hops.** When forwarding (non-empty `route`), verify that `content.from` is not the local peer and that the message arrived from the expected previous hop in the route.

3. **Tighten rate limiting.** Key the forward rate limiter on the actual sending session ID, not on the unauthenticated `content.from`.

---

### Proof of Concept

Setup: three nodes A–B–C, all connected to each other. B is the relay under test.

1. A connects to B over the HolePunching protocol.
2. A crafts a `ConnectionRequestDelivered` message:
   - `from` = C's peer ID (obtained from any peer exchange or discovery message)
   - `to` = any peer ID that C currently has in `inflight_requests` (observable from C's `ConnectionRequest` broadcasts), or any peer ID if only testing message injection
   - `route` = `[]`
   - `listen_addrs` = `[attacker_controlled_IP:port]` (valid TCP multiaddr)
   - `sync_route` = `[]`
3. A sends the message to B.
4. **Assert**: B's `execute()` enters the `None` branch, finds `B_peer_id != C_peer_id`, calls `forward_delivered(&C_peer_id)`, resolves C's session ID from the peer registry, and delivers the message to C.
5. **Assert**: C's `execute()` enters the `else` branch (`C_peer_id == content.from`). If `content.to` is in C's `inflight_requests`, C spawns a `try_nat_traversal` task making outbound TCP connections to `attacker_controlled_IP:port`.
6. Observe the TCP SYN packets arriving at the attacker's listener — confirming forced outbound connection from C to an attacker-controlled endpoint.

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L37-40)
```rust
    fn try_from(value: &packed::ConnectionRequestDeliveredReader<'_>) -> Result<Self, Self::Error> {
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L154-176)
```rust
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L223-241)
```rust
pub(crate) fn forward_delivered(
    delivered: packed::ConnectionRequestDeliveredReader<'_>,
) -> packed::ConnectionRequestDelivered {
    let route = delivered.route();
    let message = delivered.to_entity();
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
}
```
