Audit Report

## Title
Unvalidated `sync_route` in `ConnectionRequestDelivered` Enables Attacker-Directed `ConnectionSync` Relay — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary

`DeliverdContent::try_from` parses `sync_route` as an independent list of peer IDs with no check that it is the structural reverse of `route`. Because `forward_delivered` passes `sync_route` through unchanged and `init_sync` directly converts it into the route of an outgoing `ConnectionSync`, an attacker who observes a valid `(from, to)` pair from gossip can craft a `ConnectionRequestDelivered` whose `sync_route` is entirely attacker-controlled, causing relay nodes to forward `ConnectionSync` messages toward arbitrary attacker-chosen peers.

## Finding Description

**Root cause — no reverse-route invariant enforced during parsing.**
`DeliverdContent::try_from` parses `sync_route` as an independent byte-vector of peer IDs. [1](#0-0) 
The only structural guard shared between `route` and `sync_route` is a length cap against `MAX_HOPS`. [2](#0-1) 
No check verifies that `sync_route` equals `route` reversed, which is the invariant established by the legitimate destination node in `init_delivered`. [3](#0-2) 

**`forward_delivered` — `sync_route` copied verbatim.**
When a relay node forwards the message, it strips one entry from `route` but leaves `sync_route` completely unchanged. [4](#0-3) 

**`init_sync` — attacker-controlled `sync_route` becomes the `ConnectionSync` route.**
When the message reaches the `from` node with an empty `route`, `init_sync` takes `sync_route` (minus its last element) and sets it as the route of the outgoing `ConnectionSync`. [5](#0-4) 

**`inflight_requests` guard — necessary but bypassable.**
The `from` node gates on an active in-flight entry for `content.to`. [6](#0-5) 
However, `ConnectionRequest` messages are broadcast via gossip to `sqrt(total_peers)` nodes. [7](#0-6) 
Any peer that receives this gossip learns the `(from, to)` pair and its 5-minute validity window. [8](#0-7) 

**`forward_rate_limiter` — insufficient.**
The rate limiter keys on `(from, to, item_id)` where `item_id` is a fixed constant per message type. [9](#0-8) 
An attacker observing N distinct `(from, to)` pairs can send N crafted messages per second, each causing up to `MAX_HOPS=6` forwarding hops. [10](#0-9) 

**`ConnectionSyncProcess::execute` — forwards to attacker-chosen peer.**
When R receives the resulting `ConnectionSync{route=[X,Y]}`, it calls `forward_sync(&Y)` — sending to Y, an attacker-chosen peer, not the legitimate return path. [11](#0-10) 

## Impact Explanation

Each crafted `ConnectionRequestDelivered` causes relay nodes to perform peer-registry lookups, `send_message_to` calls, and consume outbound bandwidth forwarding a `ConnectionSync` toward attacker-chosen peers across up to 6 hops. With N observable `(from, to)` pairs (proportional to network size, since gossip reaches `sqrt(total_peers)` nodes per originator), the attacker sustains N forwarding chains per second from a single P2P connection. This constitutes a low-cost bandwidth amplification attack capable of causing **CKB network congestion with few costs**, matching the **High (10001–15000 points)** impact class.

## Likelihood Explanation

- Requires only a standard P2P connection to one relay node; no cryptographic material or privileged role needed.
- `ConnectionRequest` gossip is observable by any peer in the `sqrt(total_peers)` broadcast set, making `(from, to)` pairs and their 5-minute windows readily discoverable.
- The exploit is repeatable across all observed pairs simultaneously, bypassing the per-tuple rate limiter.
- The attacker only needs Y (the target of the forged `sync_route`) to be a peer connected to R, which is discoverable via standard P2P peer exchange.

## Recommendation

In `execute()` or in `DeliverdContent::try_from`, validate that `sync_route` is exactly the reverse of `route` before processing or forwarding. Reject with a ban-worthy status if the invariant is violated. Alternatively, strip `sync_route` from the wire entirely and have each relay node reconstruct it locally by appending its own peer ID as it forwards `ConnectionRequestDelivered` — mirroring how `forward_request` builds `route` on the forward path. [12](#0-11) 

## Proof of Concept

1. Connect attacker peer E to relay R; R is connected to node A.
2. Wait for A to broadcast `ConnectionRequest{from=A, to=B}` via gossip; E (or R) observes the `(A, B)` pair.
3. E sends to R: `ConnectionRequestDelivered{from=A, to=B, route=[A], sync_route=[E, E, E], listen_addrs=[valid_addr]}`.
4. R: `route.last()=A` → calls `forward_delivered` (strips route to `[]`) → sends to A.
5. A: `route` empty, `self==from`, `inflight_requests` contains B → calls `respond_sync` → `init_sync` builds `ConnectionSync{from=A, to=B, route=[E,E]}` → sends to R (the peer A received the message from).
6. R: `ConnectionSyncProcess::execute`, `route.last()=E` → calls `forward_sync(&E)` → sends `ConnectionSync` to E (attacker-chosen).
7. Assert: R forwarded `ConnectionSync` toward E, not toward A (the legitimate return path), confirming attacker-directed relay.
8. Repeat with all observed `(from, to)` pairs to sustain N forwarding chains/second.

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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L160-164)
```rust
                    let request_start = self.protocol.inflight_requests.remove(&content.to);

                    match request_start {
                        Some(start) => {
                            let res = self.respond_sync(content.from).await;
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L171-187)
```rust
pub(crate) fn forward_request(
    request: packed::ConnectionRequestReader<'_>,
    current_id: &PeerId,
) -> packed::ConnectionRequest {
    let max_hops: u8 = request.max_hops().into();
    let message = request.to_entity();
    let new_route = message
        .route()
        .as_builder()
        .push(current_id.as_bytes())
        .build();
    message
        .as_builder()
        .max_hops(max_hops.saturating_sub(1))
        .route(new_route)
        .build()
}
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L189-221)
```rust
pub(crate) fn init_delivered(
    request: packed::ConnectionRequestReader<'_>,
    listen_addrs: packed::AddressVec,
) -> packed::ConnectionRequestDelivered {
    let route = request.route();
    let message = request.to_entity();
    let new_route = packed::BytesVec::new_builder()
        .extend(
            message
                .route()
                .into_iter()
                .take(route.len().saturating_sub(1)),
        )
        .build();
    let sync_route = packed::BytesVec::new_builder()
        .extend(
            message
                .route()
                .into_iter()
                .collect::<Vec<_>>()
                .into_iter()
                .rev()
                .collect::<Vec<_>>(),
        )
        .build();
    packed::ConnectionRequestDelivered::new_builder()
        .from(message.from())
        .to(message.to())
        .route(new_route)
        .sync_route(sync_route)
        .listen_addrs(listen_addrs)
        .build()
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L98-104)
```rust
        match content.route.last() {
            Some(next_peer_id) => self.forward_sync(next_peer_id).await,
            None => {
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.to {
                    // forward the message to the `to` peer
                    self.forward_sync(&content.to).await
```
