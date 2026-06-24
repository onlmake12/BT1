Audit Report

## Title
Unbounded Outbound Amplification via `forward_rate_limiter` Key Bypass in `ConnectionRequestProcess::execute` — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary

A single unprivileged peer session can drive the victim node to emit `30 * floor(sqrt(N))` outbound hole-punching messages per second by sending 30 `ConnectionRequest` messages per second, each carrying a distinct random `to` PeerId absent from the peer registry. The `forward_rate_limiter` is keyed on `(from, to, msg_item_id)` and is trivially bypassed by rotating the `to` field, while the outer `rate_limiter` only caps inbound rate at 30/sec per session and does not cap outbound fan-out. The `forward_rate_limiter` HashMap also grows unboundedly during an active attack session since `retain_recent()` is only called on disconnect.

## Finding Description

**Outer rate limiter** is keyed by `(session_id, msg.item_id())`. [1](#0-0) 

For `ConnectionRequest`, `item_id()` always returns `0`. [2](#0-1) 

This allows exactly 30 `ConnectionRequest` messages per second per session — it is the only per-session inbound cap. [3](#0-2) 

**Inner forward rate limiter** is keyed by `(content.from, content.to, self.msg_item_id)`. [4](#0-3) 

Its quota is 1/sec per unique key. [5](#0-4) 

An attacker who rotates `to` to a fresh random PeerId on every message creates a brand-new key each time, so this limiter is never triggered.

**Fan-out in `forward_message`** when `to` is not in the peer registry: the `None` branch calls `filter_broadcast` to `floor(sqrt(N))` peers, where `N = peers().len()`. [6](#0-5) 

**HashMap memory growth**: `retain_recent()` on `forward_rate_limiter` is only called on peer disconnect, not periodically, so the HashMap accumulates one entry per unique `(from, to_i, 0)` key for the entire duration of the attack session. [7](#0-6) 

**Complete attack call chain:**
```
received()
  → rate_limiter.check_key((session_id, 0))          ← passes, ≤30/sec
  → ConnectionRequestProcess::execute()
      → forward_rate_limiter.check_key((from, to_unique_N, 0))  ← always passes (new key)
      → forward_message()
          → peer_registry.get_key_by_peer_id(to_unique_N) → None
          → filter_broadcast(sqrt(N) peers)            ← fan-out
```

## Impact Explanation

With `N` connected peers and one attacker session, outbound messages/sec = `30 * floor(sqrt(N))`. At N=10,000 this is 3,000 outbound hole-punching messages/sec from a single session; multiple attacker sessions multiply the effect linearly. This congests the victim's outbound send queue and imposes forwarding load on all `sqrt(N)` downstream peers, matching the allowed CKB bounty impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs** (10001–15000 points). The secondary unbounded HashMap growth adds a memory exhaustion vector over sustained attack.

## Likelihood Explanation

The attack requires only a standard P2P connection — no authentication, no stake, no proof-of-work. The attacker connects as a normal peer and sends well-formed `ConnectionRequest` messages with random 32-byte `to` PeerIds at 30/sec. The `to` field is arbitrary `Bytes` with no cryptographic verification required to trigger the forwarding path. This is trivially scriptable and repeatable indefinitely.

## Recommendation

1. **Add a per-session forwarding budget**: Replace or supplement the `forward_rate_limiter` with a key of `(session_id, msg_item_id)` capped at 1–5 forwards/sec, bounding fan-out to `5 * sqrt(N)` regardless of `to` rotation.
2. **Validate `to` PeerId before forwarding**: Drop or strictly rate-limit `ConnectionRequest` messages whose `to` PeerId is unknown to the local peer store, since unknown `to` is the only case that triggers the expensive `filter_broadcast` path.
3. **Cap or periodically prune the `forward_rate_limiter` HashMap**: Call `retain_recent()` on a timer (e.g., in the `notify` handler) rather than only on disconnect, and enforce a maximum entry count to prevent unbounded memory growth.

## Proof of Concept

```rust
// Pseudocode unit test
let victim = MockNode::new(n_peers = 10_000);
let attacker_session = victim.connect_peer();

for i in 0..30 {
    let unique_to = PeerId::random(); // never in registry
    attacker_session.send(ConnectionRequest {
        from: attacker_peer_id,
        to: unique_to,
        max_hops: 6,
        route: vec![],
        listen_addrs: vec![valid_addr],
    });
}
// After 1 second:
// assert_eq!(victim.outbound_message_count(), 30 * 10_000_usize.isqrt());
// assert_eq!(victim.outbound_message_count(), 3000);
// assert!(victim.forward_rate_limiter.len() >= 30); // unbounded growth
```

The outer `rate_limiter` passes all 30 messages (keyed on `(session_id, 0)`). The `forward_rate_limiter` passes all 30 (each has a unique `(from, to_i, 0)` key). Each triggers `filter_broadcast` to `floor(sqrt(10000)) = 100` peers. Total outbound = 3,000 messages in one second from one session.

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L66-70)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
        debug!("HolePunching.disconnected session={}", context.session.id);
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

**File:** network/src/protocols/hole_punching/mod.rs (L249-252)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```

**File:** util/gen-types/src/generated/protocols.rs (L5548-5554)
```rust
    pub fn item_id(&self) -> molecule::Number {
        match self {
            HolePunchingMessageUnion::ConnectionRequest(_) => 0,
            HolePunchingMessageUnion::ConnectionRequestDelivered(_) => 1,
            HolePunchingMessageUnion::ConnectionSync(_) => 2,
        }
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
