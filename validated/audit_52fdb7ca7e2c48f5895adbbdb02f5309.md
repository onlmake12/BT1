Looking at the exact code path in `network/src/protocols/hole_punching/component/connection_request.rs` and `network/src/protocols/hole_punching/mod.rs`, I can trace the full exploit path.

---

### Title
Superlinear Outbound Message Amplification via `ConnectionRequest` Broadcast Path — (`network/src/protocols/hole_punching/component/connection_request.rs`)

### Summary

A single unprivileged remote peer can cause a victim node to emit `30 * sqrt(N)` outbound hole-punching messages per second by sending 30 `ConnectionRequest` messages per second, each with a distinct attacker-chosen `content.to` peer ID that is absent from the local peer registry. The `forward_rate_limiter` is keyed on `(from, to, msg_item_id)` and is trivially bypassed by rotating `to` values. Each message that misses the registry falls into the `filter_broadcast` path that fans out to `sqrt(N)` peers.

### Finding Description

**Outer rate limiter** — keyed `(PeerIndex, msg_item_id)`, allows 30 `ConnectionRequest` messages per second per session: [1](#0-0) 

**Forward rate limiter** — keyed `(content.from, content.to, msg_item_id)`, allows 1 forward per second per `(from, to)` pair: [2](#0-1) 

**Broadcast path** — triggered when `content.to` is not found in the peer registry, fans out to `sqrt(N)` peers: [3](#0-2) 

The attacker sends 30 messages/sec, each with a **different** `content.to` peer ID (all absent from the registry). Each message creates a fresh `(from, to)` key in the `forward_rate_limiter`, so all 30 pass. Each triggers a `filter_broadcast` to `sqrt(N)` peers. Total outbound messages per second from the victim: `30 * sqrt(N)`.

The `forward_rate_limiter` quota is set to 1/sec: [4](#0-3) 

But it only limits the **same** `(from, to)` pair — it does not bound the total number of distinct pairs an attacker can introduce per second.

### Impact Explanation

With N=100 connected peers, one attacker peer causes 300 outbound messages/sec. With N=400, it causes 600/sec. The amplification factor grows as `sqrt(N)`, violating the invariant that a single peer must not cause superlinear outbound message amplification. This leads to:
- Network bandwidth exhaustion on the victim node
- Downstream peer congestion (each of the `sqrt(N)` forwarded-to peers receives the message and may re-broadcast)

### Likelihood Explanation

The path requires only a valid P2P connection and the ability to send well-formed `ConnectionRequest` messages with arbitrary `to` peer IDs. No privilege, key, or special role is needed. The `to` field is fully attacker-controlled and is not validated against any allowlist before the registry lookup. [5](#0-4) 

### Recommendation

Replace the per-`(from, to)` forward rate limiter with a **per-sender aggregate** rate limiter that caps the total number of broadcasts triggered by a single `PeerIndex` per second (e.g., 1–2 broadcasts/sec per peer regardless of how many distinct `to` values are used). Alternatively, add a global cap on the total number of `filter_broadcast` calls per second across all senders.

### Proof of Concept

1. Connect one attacker peer to a victim node that has N=100 connected peers.
2. Send 30 `ConnectionRequest` messages per second, each with a unique random `content.to` peer ID not present in the victim's registry.
3. Measure outbound `HolePunching` message rate on the victim.
4. Assert: observed rate ≈ 300 messages/sec (30 × sqrt(100)), not ≤ 30 messages/sec.

The outer rate limiter passes all 30 (keyed by session, not `to`). The forward rate limiter passes all 30 (each has a unique `(from, to)` key). Each triggers `filter_broadcast` to `sqrt(100) = 10` peers. [6](#0-5)

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

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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
