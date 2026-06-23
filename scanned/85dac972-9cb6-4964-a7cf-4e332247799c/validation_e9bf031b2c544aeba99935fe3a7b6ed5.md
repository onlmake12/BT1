Looking at the full code flow across the relevant files to trace the attack path precisely.

### Title
Unvalidated `sync_route` in `ConnectionRequestDelivered` Allows Attacker-Directed `ConnectionSync` Relay — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

---

### Summary

The `ConnectionRequestDelivered` message carries a `sync_route` field that is supposed to be the structural reverse of `route` (set by the legitimate destination node via `init_delivered`). No relay node or the originating `from` node ever validates this invariant. An attacker who can observe in-flight `ConnectionRequest` gossip can craft a `ConnectionRequestDelivered` with a fabricated `sync_route`, causing the `from` node to emit a `ConnectionSync` whose route is entirely attacker-controlled, and causing relay nodes to forward that sync toward attacker-chosen peers.

---

### Finding Description

**Parsing — no reverse-route check**

`DeliverdContent::try_from` parses `sync_route` as an independent list of peer IDs with no relationship check against `route`: [1](#0-0) 

The only structural guard applied to both fields is a shared length cap: [2](#0-1) 

**Relay forwarding — `sync_route` passed through verbatim**

`forward_delivered` strips one entry from `route` but copies `sync_route` unchanged into the forwarded message: [3](#0-2) 

**`init_sync` — attacker-controlled `sync_route` becomes the `ConnectionSync` route**

When the message reaches the `from` node with an empty `route`, `init_sync` directly converts `sync_route` into the route of the outgoing `ConnectionSync`: [4](#0-3) 

**`inflight_requests` guard — necessary but bypassable**

The `from` node does gate on an active in-flight entry: [5](#0-4) 

However, `ConnectionRequest` messages are **broadcast via gossip** to `sqrt(total_peers)` nodes in `notify()`: [6](#0-5) 

Any connected peer can observe these broadcasts, learn the `(from, to)` pair, and time the crafted `ConnectionRequestDelivered` within the 5-minute `TIMEOUT` window. [7](#0-6) 

---

### Impact Explanation

A relay node R that receives the crafted `ConnectionRequestDelivered` (with `route=[A]`, `sync_route=[X,Y,Z]`) forwards it to A. A emits a `ConnectionSync` with `route=[X,Y]` back to R. R then attempts to forward that sync to Y — an attacker-chosen peer — consuming a `send_message_to` call, a peer-registry lookup, and outbound bandwidth for each hop up to `MAX_HOPS=6`. [8](#0-7) 

The `forward_rate_limiter` caps this at 1 message/second per `(from, to, item_id)` tuple, but the attacker can use multiple observed `(from, to)` pairs simultaneously. [9](#0-8) 

---

### Likelihood Explanation

- Attacker needs only a standard P2P connection to one relay node.
- `ConnectionRequest` gossip is observable by any connected peer, making `(from, to)` pairs and their 5-minute validity windows known.
- No cryptographic material, privileged role, or majority hashpower is required.
- The exploit is repeatable across multiple observed pairs.

---

### Recommendation

In `execute()` (or in `DeliverdContent::try_from`), validate that `sync_route` is exactly the reverse of `route` before processing or forwarding. Reject the message with a ban-worthy status if the invariant is violated. Alternatively, ignore the `sync_route` field from the wire entirely and have each relay node reconstruct it locally (as it already knows its own peer ID and the path taken).

---

### Proof of Concept

1. Connect attacker peer E to relay R; R is connected to node A.
2. Observe A broadcasting `ConnectionRequest{from=A, to=B, ...}` via gossip.
3. E sends to R: `ConnectionRequestDelivered{from=A, to=B, route=[A], sync_route=[X,Y,Z], listen_addrs=[valid_addr]}`.
4. R calls `forward_delivered` → strips `route` to `[]` → sends to A.
5. A: `route` empty, `self==from`, `inflight_requests` contains B → calls `init_sync` → emits `ConnectionSync{from=A, to=B, route=[X,Y]}` to R.
6. R's `ConnectionSyncProcess::execute`: `route.last()=Y` → calls `forward_sync(&Y)` → sends to Y (attacker-chosen).
7. Assert: R forwarded `ConnectionSync` toward Y, not toward A (the legitimate return path).

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L73-80)
```rust
        let sync_route = value
            .sync_route()
            .iter()
            .map(|peer_id| {
                PeerId::from_bytes(peer_id.raw_data().to_vec())
                    .map_err(|_| StatusCode::InvalidRoute)
            })
            .collect::<Result<Vec<_>, _>>()?;
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L130-132)
```rust
        if content.route.len() > MAX_HOPS as usize || content.sync_route.len() > MAX_HOPS as usize {
            return StatusCode::InvalidRoute.with_context("the route length is too long");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L160-165)
```rust
                    let request_start = self.protocol.inflight_requests.remove(&content.to);

                    match request_start {
                        Some(start) => {
                            let res = self.respond_sync(content.from).await;
                            if !res.is_ok() {
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L243-261)
```rust
pub(crate) fn init_sync(
    delivered: packed::ConnectionRequestDeliveredReader<'_>,
) -> packed::ConnectionSync {
    let sync_route = delivered.sync_route();
    let message = delivered.to_entity();
    let new_route = packed::BytesVec::new_builder()
        .extend(
            message
                .sync_route()
                .into_iter()
                .take(sync_route.len().saturating_sub(1)),
        )
        .build();
    packed::ConnectionSync::new_builder()
        .from(message.from())
        .to(message.to())
        .route(new_route)
        .build()
}
```

**File:** network/src/protocols/hole_punching/mod.rs (L23-23)
```rust
pub(crate) const MAX_HOPS: u8 = 6;
```

**File:** network/src/protocols/hole_punching/mod.rs (L173-175)
```rust
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/mod.rs (L223-235)
```rust
                    // Broadcast to a number of nodes equal to the square root of the total connection count using gossip.
                    let mut total = status.total.isqrt();
                    let _ignore = context
                        .filter_broadcast(
                            TargetSession::Filter(Box::new(move |_| {
                                total = total.saturating_sub(1);
                                total != 0
                            })),
                            proto_id,
                            conn_req.as_bytes(),
                        )
                        .await;
                    inflight.push(to_peer_id);
```

**File:** network/src/protocols/hole_punching/mod.rs (L255-257)
```rust
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```
