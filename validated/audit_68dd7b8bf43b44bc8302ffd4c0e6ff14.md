Audit Report

## Title
Unbounded √N Gossip Fan-Out with Rotatable Rate-Limiter Key Enables O(N^(3/2)) Message Amplification — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
Any unprivileged peer can send `ConnectionRequest` messages with arbitrary `(from, to)` PeerID pairs to a CKB node. When the `to` peer is not locally known, the receiving node fans out to √N neighbors via `filter_broadcast`. The `forward_rate_limiter` is keyed per-node on `(from, to, msg_item_id)` where `msg_item_id` is the union variant index — a compile-time constant of `0` for `ConnectionRequest`. An attacker rotating `(from, to)` pairs at 30 req/s resets the limiter on every node in the network, producing O(N · √N) total forwarded messages per second from a single cheap connection.

## Finding Description

**Root cause:** The `forward_rate_limiter` key includes `msg_item_id` which is always `0` for `ConnectionRequest`, making the effective key `(from, to)` only. Since both fields are attacker-controlled arbitrary bytes, the limiter is trivially bypassed by pair rotation.

**Code path:**

1. **Outer rate limiter** (`mod.rs` L95–107): keyed on `(session_id, msg.item_id())` at 30 req/s per connection. Passes for 30 distinct messages/second from the attacker's single session. [1](#0-0) 

2. **`execute()` dispatches to `forward_message`** (`connection_request.rs` L145–152): when `self_peer_id != content.to` and `max_hops > 0`. [2](#0-1) 

3. **Route check is path-local, not global** (`connection_request.rs` L128–130): `content.route.contains(self_peer_id)` only drops the message if the node's own ID appears in the specific forwarded copy's route. A node receiving the same `(from, to)` via a different path has a different route and passes this check. [3](#0-2) 

4. **`forward_rate_limiter` key includes constant `msg_item_id`** (`connection_request.rs` L132–143): The key is `(content.from, content.to, self.msg_item_id)`. For `ConnectionRequest`, `msg_item_id` is always `0` (union variant index). Rotating `(from, to)` pairs produces a fresh key on every node in the network. [4](#0-3) 

5. **√N fan-out when `to` is unknown** (`connection_request.rs` L273–305): The `None` branch calls `filter_broadcast` to `peers().len().isqrt()` neighbors with no global deduplication across paths. [5](#0-4) 

6. **`forward_request` decrements `max_hops` and appends node to route** (`component/mod.rs` L171–187): `MAX_HOPS = 6` allows up to 6 hops of fan-out. [6](#0-5) [7](#0-6) 

**Why existing checks fail:**
- The outer `rate_limiter` (30 req/s per session) limits the attacker's injection rate but does not prevent amplification — it is the amplification factor that is the problem.
- The `forward_rate_limiter` (1 req/s per `(from, to, 0)` per node) would stop repeated forwarding of the *same* pair, but the attacker rotates pairs freely since both `from` and `to` are arbitrary bytes requiring only syntactic validity as `PeerId`.
- The route check prevents loops only along a single forwarding path; it does not deduplicate across the multiple paths that arise from √N fan-out.

## Impact Explanation

This matches the High impact class: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

For N = 10,000 nodes (each with ~100 connections, √N ≈ 100):
- 1 attacker packet → up to ~1,000,000 forwarded messages across the network.
- At 30 req/s with rotating `(from, to)` pairs: ~30,000,000 messages/second from one cheap connection.
- Each forwarded message is a full serialized `ConnectionRequest` with listen addresses, imposing real CPU and bandwidth cost on every node.

## Likelihood Explanation

- Requires only a standard P2P connection to any honest CKB node — no privilege, no PoW, no key material.
- The `HolePunching` protocol is enabled by default on production nodes.
- The `to` PeerID only needs to pass `PeerId::from_bytes` validation; it does not need to correspond to any real peer.
- The attack is repeatable at 30 req/s with rotating `(from, to)` pairs, sustaining the amplification indefinitely.
- The `msg_item_id` constant in the rate-limiter key provides zero per-message discrimination, making the limiter trivially bypassable.

## Recommendation

1. **Add a node-global seen-message cache** keyed on a hash of `(from, to, nonce)` where `nonce` is a random value set by the originator and preserved through forwarding. Drop any message whose key is already in the cache, regardless of the path it arrived on.
2. **Alternatively, remove `msg_item_id` from the `forward_rate_limiter` key** and key it solely on `(from, to)`. This is already the effective behavior, but making it explicit and ensuring it applies globally (not just per-path) would prevent the rotation bypass.
3. **Cap `max_hops` at a lower value** (e.g., 3) to reduce the reachable amplification depth.
4. **Consider requiring the `to` PeerID to be a known peer** before forwarding, or adding a proof-of-work/stake requirement to `ConnectionRequest` messages to raise the cost of injection.

## Proof of Concept

```
1. Attacker connects to node A (standard P2P handshake).
2. Attacker sends at 30 req/s, each with a fresh (from_i, to_i):
     ConnectionRequest {
       from=<valid_random_peer_id_i>,
       to=<unknown_peer_id_i>,
       max_hops=6,
       listen_addrs=[<valid_tcp_addr>],
       route=[]
     }
3. Node A: route check passes (A not in []), forward_rate_limiter passes
   (first (from_i, to_i, 0) on A), to_i not found →
   filter_broadcast to √N neighbors with route=[A], max_hops=5.
4. Each of those √N nodes: route check passes (they are not in [A]),
   forward_rate_limiter passes (first time on each), to_i not found →
   filter_broadcast to √N more neighbors.
5. This repeats for up to 6 hops. Each node forwards at most once per
   (from_i, to_i) pair (rate limiter), but there are up to N nodes,
   each sending to √N peers. Total messages ≈ N · √N per attacker packet.
6. Immediately send the next packet with (from_{i+1}, to_{i+1}) — each
   node's rate limiter resets for the new key, producing another N · √N wave.
7. Repeat at 30 req/s. Instrument message counters across all nodes to
   confirm total received messages ≈ 30 · N · √N per second.
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L279-305)
```rust
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
