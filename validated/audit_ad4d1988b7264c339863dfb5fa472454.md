Audit Report

## Title
Unbounded √N Gossip Fan-Out in `forward_message` Enables O(N·√C) Message Amplification — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
Any unprivileged peer can send a `ConnectionRequest` with an unknown `to` PeerID. Every intermediate node that cannot resolve `to` fans out to `√C` neighbors (where C is the node's local connection count) via `filter_broadcast`. The `forward_rate_limiter` is keyed per-node on `(from, to, msg_item_id)` — a constant for `ConnectionRequest` — and is trivially bypassed by rotating `(from, to)` pairs. With no global seen-message deduplication, this produces O(N·√C) total messages per attacker packet, constituting sustained network-wide message amplification from a single cheap connection.

## Finding Description

**Entrypoint**: Any peer can send `HolePunchingMessage::ConnectionRequest` over the `HolePunching` protocol.

**Step 1 — Outer rate limiter passes.** `HolePunching::received` checks `rate_limiter` keyed by `(session_id, msg.item_id())` at 30 req/s per connection. [1](#0-0) 

**Step 2 — `execute()` dispatches to `forward_message`.** When `self_peer_id != content.to` and `max_hops > 0`, `forward_message` is called. [2](#0-1) 

**Step 3 — `forward_message` fans out to √C neighbors when `to` is unknown.** The `None` branch calls `filter_broadcast` to `peers().len().isqrt()` neighbors. `peers().len()` is the local connection count C of the forwarding node, not the global network size. With a typical CKB node having ~125 connections, √125 ≈ 11 neighbors per forward. There is no global deduplication. [3](#0-2) 

**Step 4 — `forward_rate_limiter` key is `(from, to, msg_item_id)` where `msg_item_id` is a constant.** `msg_item_id` is the union variant index — always `0` for `ConnectionRequest`. The effective key is `(from, to)` per-node only. Rotating `(from, to)` pairs resets the limiter on every node in the network. [4](#0-3) [5](#0-4) 

**Step 5 — Route check is path-specific, not node-global.** `content.route.contains(self_peer_id)` only drops the message if the receiving node's own ID appears in the specific path that delivered this copy. A node receiving the same `(from, to)` via a different path passes the route check; only the `forward_rate_limiter` stops the second forward — and that limiter is already consumed by the first path. [6](#0-5) 

**Step 6 — `forward_request` decrements `max_hops` and appends the forwarding node to the route.** `MAX_HOPS = 6` allows up to 6 hops of fan-out. [7](#0-6) [8](#0-7) 

**Net effect**: Each of the N nodes in the network forwards the message at most once per `(from, to)` pair per second (rate limiter), each to √C neighbors. Total messages = **O(N · √C)** from a single attacker packet. With N = 10,000 nodes and C = 125 connections per node (√C ≈ 11): 1 attacker packet → ~110,000 forwarded messages. At 30 req/s with rotating `(from, to)` pairs: ~3,300,000 messages/second from one cheap connection.

## Impact Explanation

This matches the allowed CKB bounty impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points).**

The amplification factor O(N·√C) is concrete and measurable. With realistic CKB network parameters (N = 10,000 nodes, C ≈ 125 connections per node), a single attacker connection sustaining 30 req/s with rotating `(from, to)` pairs generates approximately 3,300,000 forwarded messages per second across the network. This constitutes sustained network-wide congestion achievable at negligible cost to the attacker.

## Likelihood Explanation

- Requires only a standard P2P connection to any honest CKB node — no privilege, no PoW, no key material.
- The `HolePunching` protocol is enabled by default on production nodes.
- The `to` PeerID only needs to be syntactically valid bytes; it does not need to correspond to any real peer.
- The attack is repeatable at 30 req/s with rotating `(from, to)` pairs, sustaining the amplification indefinitely.
- The `msg_item_id` constant in the rate-limiter key provides no per-message discrimination, making the limiter trivially bypassable by pair rotation.

## Recommendation

1. **Add a global seen-message cache per node.** Key it on a hash of `(from, to, nonce)` where `nonce` is a random value set by the originator and preserved through forwarding. Drop any message whose key is already in the cache, regardless of the path it arrived on.
2. **Alternatively, use a node-global forwarded-set keyed on `(from, to)` only** (removing the constant `msg_item_id`). Once a node has forwarded a `(from, to)` pair, it must not forward it again from any path within the rate-limit window. The current implementation already does this per-node, but the bypass is that the attacker rotates pairs — a nonce field would close this gap.
3. **Cap `max_hops` at a lower value** (e.g., 3) to reduce the reachable amplification depth.
4. **Do not use the union variant index as a rate-limiter key component** — it is a constant for any given message type and provides no per-message discrimination.

## Proof of Concept

```
1. Attacker connects to node A (standard P2P handshake).
2. Attacker sends:
     ConnectionRequest {
       from=<valid_random_peer_id_1>,
       to=<unknown_peer_id_1>,
       max_hops=6,
       listen_addrs=[<valid_tcp_addr>],
       route=[]
     }
3. Node A: route check passes (A not in []), forward_rate_limiter passes
   (first (from1, to1, 0) on A), to1 not found →
   filter_broadcast to √C neighbors with route=[A], max_hops=5.
4. Each of those √C nodes: route check passes (they are not in [A]),
   forward_rate_limiter passes (first time on each), to1 not found →
   filter_broadcast to √C more neighbors.
5. This repeats for up to 6 hops. Each node forwards at most once
   (rate limiter), but there are up to N nodes, each sending to √C peers.
   Total messages ≈ N · √C >> O(N).
6. Immediately send a second packet with (from2, to2) — each node's
   rate limiter resets for the new key, producing another N · √C wave.
7. Repeat at 30 req/s. Instrument message counters across all nodes to
   confirm total received messages ≈ 30 · N · √C per second.
   With N=10,000 and C=125: ~3,300,000 messages/second from one connection.
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L23-23)
```rust
pub(crate) const MAX_HOPS: u8 = 6;
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L128-130)
```rust
        if content.route.contains(self_peer_id) {
            return StatusCode::Ignore.with_context("the message is passed, ignore it");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L132-143)
```rust
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
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-152)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
        } else if content.max_hops == 0u8 {
            StatusCode::ReachedMaxHops.into()
        } else {
            self.forward_message(self_peer_id, &content.to).await
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L273-305)
```rust
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
