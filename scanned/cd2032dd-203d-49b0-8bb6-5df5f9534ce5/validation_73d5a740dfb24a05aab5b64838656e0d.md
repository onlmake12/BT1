### Title
Unbounded `forward_rate_limiter` Memory Growth and Network Amplification via Unique `(from, to)` Pairs in `ConnectionRequestProcess::execute` — (`network/src/protocols/hole_punching/mod.rs`, `connection_request.rs`)

---

### Summary

An unprivileged remote peer can exploit the two-layer rate-limiting design in the `HolePunching` protocol to cause unbounded memory growth in `forward_rate_limiter`'s `HashMapStateStore` and amplify outbound traffic to sqrt(N) peers per message, by sending 30 `ConnectionRequest` messages per second each with a unique attacker-controlled `(from, to)` pair.

---

### Finding Description

The `HolePunching` protocol uses two rate limiters:

**Outer `rate_limiter`** — keyed by `(PeerIndex, item_id)` at 30 req/sec: [1](#0-0) [2](#0-1) 

Since `item_id()` for `ConnectionRequest` is always the constant `0`: [3](#0-2) 

This allows exactly 30 `ConnectionRequest` messages per second from a single session.

**Inner `forward_rate_limiter`** — keyed by `(PeerId, PeerId, u32)` at 1 req/sec: [4](#0-3) [5](#0-4) 

The `from` and `to` fields are parsed directly from the message payload with no validation that `from` matches the actual sender's peer ID: [6](#0-5) 

The inner check in `execute()`: [7](#0-6) 

Because each unique `(from_i, to_i, 0)` tuple is a **new key** in the `HashMapStateStore`, it creates a fresh bucket and passes the 1/sec check unconditionally. An attacker sending 30 messages/sec with 30 distinct `(from_i, to_i)` pairs bypasses the inner limiter entirely.

**`retain_recent()` is only called on disconnect**, never periodically: [8](#0-7) 

The `notify` handler (fired every 5 minutes) cleans up `pending_delivered` and `inflight_requests` but never calls `retain_recent()` on either rate limiter: [9](#0-8) 

**Network amplification**: when the fake `to` peer is not in the registry, `forward_message` calls `filter_broadcast` to sqrt(N) peers: [10](#0-9) 

---

### Impact Explanation

**Memory exhaustion**: Each `(from_i, to_i, 0)` entry in `HashMapStateStore` stores a `(PeerId, PeerId, u32)` key (~80 bytes) plus governor rate state (~32 bytes). At 30 entries/sec:
- After 1 hour: ~108,000 entries ≈ ~12 MB per attacker connection
- After 24 hours: ~288 MB per attacker connection
- Multiple simultaneous attackers scale this linearly

Since `retain_recent()` is never called during the connection lifetime, entries accumulate without bound until the peer disconnects.

**Network amplification**: With N connected peers, each of the 30 forwarded messages/sec triggers a broadcast to sqrt(N) peers. At N=100, one attacker connection generates 300 outbound forwarded messages/sec. At N=1000, it generates ~950/sec.

---

### Likelihood Explanation

The attack requires only a single persistent P2P connection — no special privileges, no PoW, no keys. The `from` and `to` fields are fully attacker-controlled with no binding to the actual session identity. The outer rate limiter's 30/sec quota is the only constraint, and it is exactly the budget the attacker exploits. The attack is trivially automatable.

---

### Recommendation

1. **Periodic `retain_recent()` in `notify`**: Call `self.forward_rate_limiter.retain_recent()` inside the `notify` handler (every 5 minutes) to evict stale entries.

2. **Validate `from` == actual sender**: Before the `forward_rate_limiter` check, verify that `content.from` matches the peer ID of the actual session (looked up from `network_state.peer_registry`). This prevents spoofed `from` values from inflating the key space.

3. **Cap `forward_rate_limiter` size**: Enforce a maximum entry count on the `HashMapStateStore` (e.g., 1000 entries total) and reject new keys when the cap is reached.

4. **Reduce outer quota or add a global forwarding budget**: The 30/sec outer quota is too permissive for a forwarding protocol; a lower per-session cap (e.g., 5/sec) combined with a global forwarding rate limit would reduce amplification.

---

### Proof of Concept

```rust
// Pseudocode: attacker sends 30 ConnectionRequest/sec with unique (from_i, to_i)
for t in 0..60 {  // 60 seconds
    for i in 0..30 {
        let from_i = PeerId::random();
        let to_i   = PeerId::random();
        send_connection_request(session, from_i, to_i);
        // outer rate_limiter: (session_id, 0) → allows (quota=30/sec)
        // forward_rate_limiter: (from_i, to_i, 0) → new key, always passes
        // → forward_message called → filter_broadcast to sqrt(N) peers
        // → forward_rate_limiter gains 1 new entry
    }
    sleep(1s);
}
// After 60s: forward_rate_limiter has 1800 entries, never cleaned
// Total forwarded: 1800 * sqrt(N) messages sent to other peers
// assert forward_rate_limiter.len() == 1800  // unbounded growth confirmed
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L45-45)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L46-46)
```rust
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```

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

**File:** network/src/protocols/hole_punching/mod.rs (L169-176)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let status = self.network_state.connection_status();

        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);

```

**File:** network/src/protocols/hole_punching/mod.rs (L251-252)
```rust
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/mod.rs (L256-257)
```rust
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-40)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L280-305)
```rust
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
