Audit Report

## Title
Unbounded `forward_rate_limiter` Memory Growth and Network Amplification via Spoofed `(from, to)` Pairs in `ConnectionRequestProcess::execute` — (`network/src/protocols/hole_punching/mod.rs`, `network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary

The `HolePunching` protocol's two-layer rate-limiting design is bypassable by a single unprivileged peer. The outer `rate_limiter` allows 30 `ConnectionRequest` messages per second per session. Because `from` and `to` are taken directly from the message payload without verifying they match the actual sender, an attacker can send 30 messages/sec each with a unique `(from_i, to_i)` pair, creating 30 new entries per second in `forward_rate_limiter`'s `HashMapStateStore` — none of which are ever evicted during the connection lifetime. Each forwarded message also triggers a `filter_broadcast` to sqrt(N) peers, amplifying outbound traffic.

## Finding Description

**Outer rate limiter** is keyed by `(PeerIndex, item_id)` at 30 req/sec. [1](#0-0) [2](#0-1) 

`item_id()` for `ConnectionRequest` is always the constant `0` (it is the first variant in the union): [3](#0-2) 

So the outer limiter key is always `(session_id, 0)` — a single bucket shared by all `ConnectionRequest` messages from that session, allowing exactly 30/sec.

**Inner `forward_rate_limiter`** is keyed by `(PeerId, PeerId, u32)` at 1 req/sec: [4](#0-3) 

The `from` and `to` fields are parsed directly from the message payload with no check that `from` matches the actual session's peer ID: [5](#0-4) 

The inner check in `execute()`: [6](#0-5) 

Each unique `(from_i, to_i, 0)` tuple is a **new key** in the `HashMapStateStore`, so it creates a fresh bucket and passes the 1/sec check unconditionally. An attacker sending 30 messages/sec with 30 distinct `(from_i, to_i)` pairs bypasses the inner limiter entirely.

**`retain_recent()` is only called on disconnect**, never periodically: [7](#0-6) 

The `notify` handler (fired every 5 minutes) cleans `pending_delivered` and `inflight_requests` but never calls `retain_recent()` on either rate limiter: [8](#0-7) 

**Network amplification**: when the fake `to` peer is not in the registry, `forward_message` calls `filter_broadcast` to sqrt(N) peers: [9](#0-8) 

## Impact Explanation

**Network amplification** matches the allowed High impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* A single persistent P2P connection generates 30 × sqrt(N) outbound forwarded messages per second. At N=100 peers, that is 300 forwarded messages/sec; at N=1000, ~950/sec. Multiple simultaneous attackers scale this linearly with no additional cost per attacker.

**Memory exhaustion** is a secondary concern: at 30 new `HashMapStateStore` entries/sec (each ~112 bytes for two `PeerId`s + `u32` + governor state), a single attacker accumulates ~12 MB/hour and ~288 MB/day without any eviction, which can eventually crash the node (High: *"Vulnerabilities which could easily crash a CKB node"*).

## Likelihood Explanation

The attack requires only a single standard P2P connection — no special privileges, no proof-of-work, no cryptographic keys. The `from` and `to` fields are fully attacker-controlled with no binding to the actual session identity. The outer rate limiter's 30/sec quota is the exact budget the attacker exploits. The attack is trivially automatable and repeatable for the entire duration of the connection.

## Recommendation

1. **Periodic `retain_recent()` in `notify`**: Call `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify` handler (every 5 minutes) to evict stale entries and bound memory growth.

2. **Validate `from` == actual sender**: Before the `forward_rate_limiter` check in `execute()`, verify that `content.from` matches the peer ID of the actual session (looked up from `network_state.peer_registry`). This prevents spoofed `from` values from inflating the key space.

3. **Cap `forward_rate_limiter` size**: Enforce a maximum entry count on the `HashMapStateStore` (e.g., 1000 entries total) and reject new keys when the cap is reached.

4. **Reduce outer quota**: The 30/sec outer quota is too permissive for a forwarding protocol; a lower per-session cap (e.g., 5/sec) combined with a global forwarding rate limit would reduce amplification.

## Proof of Concept

```rust
// Attacker sends 30 ConnectionRequest/sec with unique (from_i, to_i) for 60 seconds
for t in 0..60 {
    for i in 0..30 {
        let from_i = PeerId::random(); // attacker-controlled, not validated
        let to_i   = PeerId::random(); // not in peer registry → triggers filter_broadcast
        send_connection_request(session, from_i, to_i);
        // outer rate_limiter: key=(session_id, 0) → allows (quota=30/sec, shared bucket)
        // forward_rate_limiter: key=(from_i, to_i, 0) → NEW key, always passes 1/sec check
        // → forward_message called → filter_broadcast to sqrt(N) peers
        // → forward_rate_limiter gains 1 new entry, never evicted
    }
    sleep(1s);
}
// After 60s:
//   forward_rate_limiter.len() == 1800  (unbounded growth confirmed)
//   Total forwarded messages == 1800 * sqrt(N)
//   At N=100: 18,000 outbound messages in 60s
//   At N=1000: ~56,900 outbound messages in 60s
```

To verify: write an integration test that connects a peer, sends 30 `ConnectionRequest` messages per second for N seconds each with `PeerId::random()` for `from`/`to`, and assert that (a) `forward_rate_limiter` internal map size equals `30 * N` after the loop, and (b) the mock `filter_broadcast` call count equals `30 * N * sqrt(peer_count)`.

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L45-46)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
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

**File:** util/gen-types/src/generated/protocols.rs (L5579-5584)
```rust
    pub fn item_id(&self) -> molecule::Number {
        match self {
            HolePunchingMessageUnionReader::ConnectionRequest(_) => 0,
            HolePunchingMessageUnionReader::ConnectionRequestDelivered(_) => 1,
            HolePunchingMessageUnionReader::ConnectionSync(_) => 2,
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
