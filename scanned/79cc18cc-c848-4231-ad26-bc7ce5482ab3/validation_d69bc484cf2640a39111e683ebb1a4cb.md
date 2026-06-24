Audit Report

## Title
`forward_rate_limiter` Bypass via Rotating `to` PeerId Enables Gossip Amplification — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
The `forward_rate_limiter` in the hole-punching protocol is keyed on `(content.from, content.to, msg_item_id)`. An attacker with a single P2P connection can send 30 `ConnectionRequest` messages per second (the outer session cap), each with a distinct random `to` PeerId, producing a unique limiter key per message and bypassing the 1/sec forwarding guard entirely. Because each unknown `to` causes the victim to call `filter_broadcast` to `floor(sqrt(K))` peers, the attacker achieves a sustained `30 × floor(sqrt(K))` outbound message amplification per second from a single inbound connection, with no ban ever triggered.

## Finding Description

**Outer session rate limiter** (`mod.rs` L95–107): keyed on `(session_id, msg.item_id())` with a quota of 30/sec. This is the only absolute cap on inbound rate from a single peer. It does not limit outbound amplification. [1](#0-0) [2](#0-1) 

**Inner `forward_rate_limiter`** (`mod.rs` L254–257): keyed on `(PeerId, PeerId, u32)` — i.e., `(content.from, content.to, msg_item_id)` — with a quota of 1/sec. The intent is to prevent repeated forwarding of the same `(from, to)` pair. [3](#0-2) [4](#0-3) 

**Bypass**: Because `to` is part of the key, each message with a fresh random `to` PeerId produces a new key `(attacker_id, to_N, 0)`. The limiter never fires. The attacker can send all 30 messages/sec through the `forward_rate_limiter` unconditionally.

**Amplification path** (`connection_request.rs` L273–305): When `to` is not found in the peer registry, `forward_message` calls `filter_broadcast` targeting `floor(sqrt(K))` peers. Since the `to` PeerId is random and unknown, this branch is always taken. [5](#0-4) 

**`forward_request` preserves `from` and `to`** (`component/mod.rs` L171–186): The forwarded message retains the original `from` and `to` fields, only decrementing `max_hops` and appending the current node to `route`. This means intermediate nodes also see a unique `(from, to_N)` key and pass their own `forward_rate_limiter`, enabling multi-hop amplification up to `MAX_HOPS = 6`. [6](#0-5) [7](#0-6) 

**No ban triggered**: `TooManyRequests = 110` is a 1xx status code. `should_ban()` only returns `Some` for codes in `400..500`. The attacker is never disconnected or banned. [8](#0-7) [9](#0-8) 

The route loop-prevention check (`route.contains(self_peer_id)`) limits re-processing by already-visited nodes but does not prevent the initial sqrt(K) fan-out per message or the multi-hop amplification to new nodes. [10](#0-9) 

## Impact Explanation

**Allowed impact: High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

A single attacker connection sustains `30 × floor(sqrt(K))` outbound hole-punching messages per second from the victim node. For K=100 peers: 300 outbound/sec; K=1000: ~930 outbound/sec; K=10000: ~3000 outbound/sec. Each of the sqrt(K) recipient nodes repeats the same amplification for up to 6 hops (MAX_HOPS), causing cascading network-wide message flooding. The attack is indefinitely sustainable with no ban, no PoW, and no privilege required — matching the "few costs" criterion for network congestion.

## Likelihood Explanation

- Requires only a single valid P2P connection to any CKB node
- No cryptographic material, no PoW, no elevated privilege
- Bypass is trivial: generate a fresh random 32-byte `to` PeerId per message
- The outer 30/sec cap is the only real throttle; it does not prevent amplification
- The attack is indefinitely repeatable since `TooManyRequests` (110) never triggers a ban

## Recommendation

1. **Re-key `forward_rate_limiter` by sender session**, not by `(from, to)` tuple. A per-session cap of N forwards/second regardless of `to` closes the bypass entirely.
2. **Add a global outbound broadcast budget** for the hole-punching protocol to cap total `filter_broadcast` calls per second across all sessions.
3. **Treat repeated `TooManyRequests` as a bannable offense**, or at minimum disconnect the session after N violations within a window.
4. **Require `to` to be a known peer** before forwarding, or limit gossip forwarding to nodes that have previously announced the `to` peer via discovery.

## Proof of Concept

```
1. Establish a single P2P connection to the victim node.
2. Loop at 30 msg/sec:
   a. Generate a fresh random 32-byte PeerId as `to` (guaranteed unknown to victim).
   b. Set `from` = attacker's own valid PeerId.
   c. Set `max_hops` = 6, `route` = [], `listen_addrs` = [one valid TCP addr].
   d. Send ConnectionRequest message.
3. Observe: victim calls filter_broadcast(floor(sqrt(K)) peers) for each message.
4. Differential test:
   - Known `to` (direct peer): 1 outbound (send_message_to).
   - Unknown `to` (random): floor(sqrt(K)) outbound (filter_broadcast).
   - Ratio = floor(sqrt(K)), confirming amplification.
5. With K=100: 30 inbound → 300 outbound/sec, sustained indefinitely, no ban.
6. Verify no ban: monitor session; attacker connection remains open throughout.
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L171-186)
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
```

**File:** network/src/protocols/hole_punching/status.rs (L21-21)
```rust
    TooManyRequests = 110,
```

**File:** network/src/protocols/hole_punching/status.rs (L99-106)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        let code = self.code() as u16;
        if (400..500).contains(&code) {
            Some(BAD_MESSAGE_BAN_TIME)
        } else {
            None
        }
    }
```
