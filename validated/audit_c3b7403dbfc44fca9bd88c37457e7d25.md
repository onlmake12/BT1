Let me check the exact rate limiter quota and `msg_item_id` semantics to be precise.

### Title
Outbound Message Amplification via `ConnectionRequest` Broadcast with Rotating Unknown `to` PeerIds — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

A single unprivileged peer connected to a CKB node can cause that node to emit `sqrt(N)` outbound `ConnectionRequest` messages for every inbound message it sends, by rotating the `to` PeerId field across requests. With 100 peers and the 30 req/sec outer rate limit, this yields a sustained 300 outbound messages/sec from one attacker connection. The `forward_rate_limiter` does not prevent this because it is keyed per `(from, to, item_id)` tuple — rotating `to` creates a fresh key each time.

---

### Finding Description

**Two rate limiters exist:**

**1. Outer `rate_limiter`** — keyed by `(session_id, msg.item_id())`, quota 30/sec: [1](#0-0) 

This limits a single peer to 30 `ConnectionRequest` messages per second total.

**2. `forward_rate_limiter`** — keyed by `(content.from, content.to, self.msg_item_id)`, quota 1/sec: [2](#0-1) 

This is intended to prevent the same `(from, to)` pair from being forwarded more than once per second. However, `msg_item_id` is the union discriminant (a constant per message type, not a per-message unique ID), so the key is effectively `(from_peer_id, to_peer_id, ConnectionRequest_type_constant)`.

**The amplification path:**

When `to` is not found in the peer registry, `forward_message` broadcasts to `sqrt(total_peers)` peers: [3](#0-2) 

The broadcast count is computed as: [4](#0-3) 

**Bypass:** The attacker sends 30 messages/sec, each with a **different random `to` PeerId** (all unknown to the node). Each message creates a fresh `(from, to_N, item_id)` key in the `forward_rate_limiter`, so all 30 pass. Each triggers `filter_broadcast` to `sqrt(N)` peers.

`forward_request` decrements `max_hops` by 1 via `saturating_sub(1)`: [5](#0-4) 

With `max_hops=1` in the original message, the forwarded copy has `max_hops=0`. Downstream peers hit the `ReachedMaxHops` branch and do not forward further: [6](#0-5) 

So the amplification is exactly one level deep — but the victim node still emits `30 × sqrt(N)` outbound messages/sec.

---

### Impact Explanation

| Peers (N) | sqrt(N) | Inbound rate | Outbound rate | Amplification |
|-----------|---------|--------------|---------------|---------------|
| 100       | 10      | 30 msg/s     | 300 msg/s     | 10×           |
| 400       | 20      | 30 msg/s     | 600 msg/s     | 20×           |
| 900       | 30      | 30 msg/s     | 900 msg/s     | 30×           |

The victim node's outbound bandwidth to its legitimate peers is consumed by attacker-induced forwarding. The attacker's cost is one P2P connection and 30 small messages/sec. The victim's cost scales with its peer count.

---

### Likelihood Explanation

- Requires only a standard P2P connection — no privilege, no key, no PoW.
- The bypass (rotating `to` PeerIds) is trivial: generate random 32-byte peer IDs.
- The outer rate limit of 30/sec is the only real constraint, and it is the amplification multiplier, not a mitigation.
- Realistic on mainnet: well-connected nodes with 100+ peers are common.

---

### Recommendation

The `forward_rate_limiter` must be keyed on the **sender session** (or at minimum on `from`), not on `(from, to)`. A per-session total forwarding quota (e.g., 1–2 forward operations/sec regardless of `to`) would cap the amplification to 1×. Alternatively, cap the total number of `filter_broadcast` calls per session per second at the node level before dispatching to `forward_message`.

---

### Proof of Concept

```
1. Connect to a CKB node with 100 peers (victim).
2. In a loop at 30 iterations/sec:
   a. Generate a random 32-byte PeerId as `to`.
   b. Send ConnectionRequest { from=<attacker_id>, to=<random>, max_hops=1, listen_addrs=[<valid_addr>], route=[] }
3. Monitor victim's outbound HolePunching message rate.
4. Expected: victim emits ~300 ConnectionRequest messages/sec to its 10 (sqrt(100)) peers,
   while attacker sends only 30/sec — a 10× amplification factor.
```

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L148-152)
```rust
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L175-186)
```rust
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
```
