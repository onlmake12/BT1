The code confirms the attack path is real and exploitable. Let me verify the exact rate-limiter key structure to be precise.

### Title
Unbounded O(sqrt(N)) Outbound Amplification via `ConnectionRequest` with Unique `to` PeerIds — (`network/src/protocols/hole_punching/mod.rs`, `network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

A single unprivileged peer session can drive the victim node to emit `30 * floor(sqrt(N))` outbound hole-punching messages per second by sending 30 `ConnectionRequest` messages per second, each carrying a distinct random `to` PeerId that is absent from the peer registry. The `forward_rate_limiter` is keyed on `(from, to, msg_item_id)` and is trivially bypassed by rotating the `to` field, while the outer `rate_limiter` only caps the inbound rate at 30/sec per session — it does not cap the outbound fan-out.

---

### Finding Description

**Outer rate limiter** (`rate_limiter`): [1](#0-0) 

Keyed by `(session_id, msg.item_id())`. `item_id()` for `ConnectionRequest` is always `0`. This allows exactly 30 `ConnectionRequest` messages per second per session — it is the only per-session inbound cap.

**Inner forward rate limiter** (`forward_rate_limiter`): [2](#0-1) 

Keyed by `(content.from, content.to, self.msg_item_id)`. Its purpose (per the comment) is to prevent the same `(from, to)` pair from being forwarded more than once per second. An attacker who rotates `to` to a fresh random PeerId on every message creates a brand-new key each time, so this limiter is never triggered.

**Fan-out in `forward_message`** when `to` is not in the peer registry: [3](#0-2) 

The `None` branch calls `filter_broadcast` to `floor(sqrt(N))` peers, where `N = peers().len()`.

**Rate limiter quota values**: [4](#0-3) 

The outer limiter allows 30/sec; the inner limiter allows 1/sec per unique key.

**Complete attack call chain:**

```
received()
  → rate_limiter.check_key((session_id, 0))   ← passes, ≤30/sec
  → ConnectionRequestProcess::execute()
      → forward_rate_limiter.check_key((from, to_unique_N, 0))  ← always passes (new key)
      → forward_message()
          → peer_registry.get_key_by_peer_id(to_unique_N) → None
          → filter_broadcast(sqrt(N) peers)   ← fan-out
```

---

### Impact Explanation

With `N` connected peers and one attacker session:

- **Outbound messages/sec** = `30 * floor(sqrt(N))`
- At N = 10,000: **3,000 outbound hole-punching messages/sec** from one session
- At N = 2,500: **1,500 outbound messages/sec**

Each forwarded message is sent to `sqrt(N)` distinct peers, congesting the victim's outbound send queue and imposing load on all `sqrt(N)` forwarding peers. Multiple attacker sessions multiply the effect linearly. The `forward_rate_limiter` HashMap also grows unboundedly (one entry per unique `to` PeerId sent), consuming memory proportional to the number of messages sent.

---

### Likelihood Explanation

The attack requires only a standard P2P connection to the victim — no authentication, no stake, no PoW. The attacker needs to:
1. Connect to the victim node (normal peer connection)
2. Send well-formed `ConnectionRequest` messages with random 32-byte `to` PeerIds at 30/sec

This is trivially achievable. The `to` field is an arbitrary `Bytes` value with no cryptographic verification required to trigger the forwarding path.

---

### Recommendation

1. **Add a per-session forwarding budget**: Track the total number of `filter_broadcast` calls (or total peers fanned-out to) per session per second, and cap it independently of the `to` PeerId. For example, a per-session `forward_rate_limiter` keyed only on `(session_id, msg_item_id)` with a quota of 1–5/sec would bound the fan-out to `5 * sqrt(N)`.

2. **Validate `to` PeerId before forwarding**: Only forward `ConnectionRequest` messages whose `to` PeerId is known to the local peer store or registry. Unknown `to` PeerIds should be dropped or subject to a much stricter rate limit, since they are the only case that triggers the expensive `filter_broadcast` path.

3. **Cap the `forward_rate_limiter` HashMap size**: Bound the number of entries to prevent unbounded memory growth from unique-key flooding.

---

### Proof of Concept

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
// assert_eq!(victim.outbound_message_count(), 30 * 100); // = 3000
// assert!(victim.forward_rate_limiter.len() >= 30); // unbounded growth
```

The outer `rate_limiter` passes all 30 messages (keyed on `(session_id, 0)`). The `forward_rate_limiter` passes all 30 (each has a unique `(from, to_i, 0)` key). Each triggers `filter_broadcast` to `floor(sqrt(10000)) = 100` peers. Total outbound = 3,000 messages in one second from one session.

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

**File:** network/src/protocols/hole_punching/mod.rs (L249-257)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

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
