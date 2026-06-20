### Title
Unbounded Traffic Amplification via `forward_rate_limiter` Bypass in Hole Punching Protocol — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

An unprivileged remote peer can bypass the `forward_rate_limiter` by sending `ConnectionRequest` messages with unique attacker-controlled `(from, to)` PeerId pairs. Because `from` is never validated against the actual session's peer ID, each message gets its own rate-limiter bucket, allowing all 30 messages/sec (the outer `rate_limiter` cap) to pass through. Each message with an unknown `to` triggers a `filter_broadcast` to sqrt(N) peers, yielding 30×sqrt(N) outbound messages per second from a single attacker connection.

---

### Finding Description

**Outer rate limiter** (`mod.rs` lines 95–107):

The per-session rate limiter is keyed by `(session_id, msg.item_id())`. [1](#0-0) 

Since `session_id` and `msg.item_id()` (the ConnectionRequest type constant) are both fixed for a single attacker connection, this allows exactly 30 `ConnectionRequest` messages per second to pass.

**Forward rate limiter** (`connection_request.rs` lines 132–143):

The forwarding rate limiter is keyed by `(content.from, content.to, self.msg_item_id)`. [2](#0-1) 

Both `content.from` and `content.to` are parsed directly from the attacker-supplied message bytes with no validation that `from` matches the actual session's peer ID: [3](#0-2) 

An attacker sending 30 messages with 30 distinct `(from, to)` pairs creates 30 independent rate-limiter buckets, each with a fresh 1/sec quota. All 30 messages pass.

**Broadcast amplification** (`connection_request.rs` lines 273–305):

When `to_peer_id` is not found in the peer registry (guaranteed for random unknown PeerIds), `filter_broadcast` is called to sqrt(N) peers: [4](#0-3) 

The `route.contains(self_peer_id)` loop-prevention check (line 128) is bypassed because the attacker sends `route=[]`. [5](#0-4) 

`forward_request` adds the current node to the route and decrements `max_hops` by 1 per hop: [6](#0-5) 

With `MAX_HOPS = 6`, the message propagates up to 6 hops deep across the network. [7](#0-6) 

---

### Impact Explanation

- **Per-node amplification**: 1 attacker connection → 30 × sqrt(N) outbound messages/sec at the directly connected node.
- **Network-wide amplification**: Each of the sqrt(N) receiving nodes also sees an unknown `to`, triggering another sqrt(N) broadcast. Over MAX_HOPS=6 hops, total network-wide messages per second scale as O(30 × N^(1 + 6×0.5)) in the worst case, though the `route` field limits revisiting individual nodes.
- **Memory exhaustion**: The `forward_rate_limiter` `HashMapStateStore` grows unboundedly as the attacker generates unique `(from, to)` pairs — one new entry per message, never evicted until `retain_recent()` is called on disconnect. [8](#0-7) 

---

### Likelihood Explanation

The attack requires only a single valid P2P session. No PoW, no keys, no privileged role. The attacker needs to:
1. Connect to any CKB node.
2. Send 30 `ConnectionRequest` messages/sec with random `from`/`to` PeerIds and a valid `listen_addrs` entry (any TCP multiaddr with the matching `from` peer ID embedded).

This is trivially scriptable and requires no special knowledge of the network topology.

---

### Recommendation

1. **Validate `from` against the session's actual peer ID**: Reject any `ConnectionRequest` where `content.from` does not match the peer ID of the sending session. This eliminates the ability to generate unique `(from, to)` pairs.
2. **Key `forward_rate_limiter` by `(session_id, to)`** instead of `(from, to, item_id)`: This bounds forwarding to 1/sec per `(session, to)` pair, regardless of the attacker-controlled `from` field.
3. **Bound the `forward_rate_limiter` map size**: Add a maximum entry count or use a fixed-size LRU structure to prevent memory exhaustion from unique-key flooding.

---

### Proof of Concept

```
1. Attacker connects to victim node (session_id = S).
2. For i in 1..30:
     from_i = PeerId::random()
     to_i   = PeerId::random()   // guaranteed not in peer registry
     addr_i = /ip4/1.2.3.4/tcp/8114/p2p/<from_i>
     Send ConnectionRequest { from: from_i, to: to_i, max_hops: 6,
                               listen_addrs: [addr_i], route: [] }
3. Outer rate_limiter: key=(S, ConnectionRequest_id), quota=30/sec → all 30 pass.
4. forward_rate_limiter: key=(from_i, to_i, ConnectionRequest_id), 30 distinct keys → all 30 pass.
5. execute(): self_peer_id != to_i, max_hops=6 > 0 → forward_message() called 30 times.
6. forward_message(): to_i not in registry → filter_broadcast to sqrt(N) peers, 30 times.
7. Result: 30 × sqrt(N) outbound messages/sec from victim node.
   With N=100: 300 outbound msgs/sec from one attacker connection.
   With N=1000: ~950 outbound msgs/sec from one attacker connection.
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L23-23)
```rust
pub(crate) const MAX_HOPS: u8 = 6;
```

**File:** network/src/protocols/hole_punching/mod.rs (L66-69)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
        debug!("HolePunching.disconnected session={}", context.session.id);
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-40)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
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
