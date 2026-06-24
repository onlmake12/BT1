Audit Report

## Title
Attacker-Controlled `from` Field Bypasses `forward_rate_limiter`, Enabling Traffic-Amplification DoS — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary

`ConnectionRequestProcess::execute()` and `ConnectionRequestDeliveredProcess::execute()` key the `forward_rate_limiter` on `(content.from, content.to, msg_item_id)`, where `content.from` is deserialized directly from the attacker-controlled wire message and never validated against the authenticated session identity. By rotating a fresh random `from` peer ID on each message, an attacker with a single TCP connection can create a new rate-limit bucket per message, bypassing the forwarding rate limiter entirely and causing the node to broadcast each crafted packet to `sqrt(N)` connected peers.

## Finding Description

**Two-layer rate limiting — only the first layer is unbypassable.**

In `mod.rs` the outer `rate_limiter` fires first, keyed on the actual authenticated `(session_id, msg_item_id)`:

```rust
// mod.rs L95-107
if self
    .rate_limiter
    .check_key(&(session_id, msg.item_id()))
    .is_err()
{ ... return; }
```

This limits a single session to 30 messages/second per message type and cannot be bypassed because `session_id` is transport-layer-authenticated.

The inner `forward_rate_limiter`, however, is keyed on attacker-supplied data:

```rust
// connection_request.rs L132-143
if self
    .protocol
    .forward_rate_limiter
    .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
    .is_err()
{ ... return StatusCode::TooManyRequests ... }
```

`content.from` is parsed entirely from the wire message at L36-38 and is never cross-checked against `self.peer` (the authenticated `PeerIndex`). By sending a distinct random `from` value in each of the 30 allowed messages per second, the attacker places each message in its own fresh bucket, so the `forward_rate_limiter` never fires.

**Forwarding amplification path.**

When `content.to` is not a directly connected peer, `forward_message()` performs a gossip broadcast:

```rust
// connection_request.rs L280-298
let mut total = self
    .protocol
    .network_state
    .with_peer_registry(|p| p.peers().len())
    .isqrt();
// ... filter_broadcast to sqrt(total) peers
```

Each of the 30 attacker messages per second triggers a broadcast to `sqrt(N)` peers, yielding `30 * sqrt(N)` outbound forwarded messages per second from a single attacker connection. The `forward_rate_limiter` was specifically designed to cap this at 1 forward per `(from, to)` pair per second; the bypass removes that cap entirely.

**Same flaw in `ConnectionRequestDeliveredProcess`.**

`DeliverdContent.from` is likewise parsed from the wire at `connection_request_delivered.rs` L38-40, and the `forward_rate_limiter` is keyed on it at L134-145 with no validation against the actual sender session.

**`pending_delivered` secondary bypass.**

When the node is the target (`self_peer_id == &content.to`), `respond_delivered()` checks `pending_delivered` keyed on `from_peer_id` (= `content.from`) at L161-167. Rotating `content.from` bypasses this deduplication guard as well, causing the node to repeatedly build and send `ConnectionRequestDelivered` responses.

## Impact Explanation

A single attacker peer can sustain 30 `ConnectionRequest` messages/second (bounded by the outer per-session limiter), each forwarded to `sqrt(N)` peers. For a node with 100 connections this is 300 forwarded messages/second from one attacker connection; for 400 connections it is 600/second. The `forward_rate_limiter` was the intended defense against exactly this amplification and is rendered ineffective. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** The attacker's cost is one TCP connection and 30 small packets/second; the network cost is O(sqrt(N)) amplified traffic per hop.

## Likelihood Explanation

Any peer connected to a CKB node with the `HolePunching` protocol enabled can trigger this immediately. No special role, no keys, and no majority hash power are required. The attacker needs only to craft `ConnectionRequest` messages with a fresh random `from` byte string on each send. The outer rate limiter (30/sec) is not a meaningful barrier — it is the intended throughput for legitimate hole-punching traffic.

## Recommendation

After deserializing `content.from`, resolve the actual peer ID of `self.peer` from the network state and reject any message where they do not match:

```rust
let actual_from = self.protocol.network_state
    .with_peer_registry(|reg| {
        reg.get_peer(self.peer)
            .and_then(|p| extract_peer_id(&p.connected_addr))
    });
if actual_from.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId
        .with_context("from peer id does not match authenticated sender");
}
```

Apply the identical fix in `ConnectionRequestDeliveredProcess`. Alternatively, re-key the `forward_rate_limiter` on `(self.peer, content.to, self.msg_item_id)` so the bucket is always tied to the authenticated session rather than the attacker-supplied field.

## Proof of Concept

1. Establish a standard P2P connection to a CKB node with `HolePunching` enabled.
2. In a tight loop (≤30 iterations/second to stay under the outer limiter), send `ConnectionRequest` messages over the `HolePunching` protocol where:
   - `from` = freshly generated random 32-byte peer ID (different each iteration)
   - `to` = any peer ID not currently connected to the node (triggers broadcast path)
   - `max_hops` = `MAX_HOPS` (6)
   - `listen_addrs` = one valid TCP multiaddr embedding the random `from` peer ID
3. Observe: each message passes `forward_rate_limiter.check_key(...)` (new bucket each time) and triggers `filter_broadcast` to `sqrt(total_peers)` connected peers.
4. Confirm: the node emits `sqrt(N)` outbound forwarded `ConnectionRequest` messages per attacker message, with no rate-limit rejection logged, demonstrating the intended 1-per-second-per-pair cap is completely bypassed. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L45-46)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L280-298)
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
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L38-40)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L134-145)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionRequestDelivered",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionRequestDelivered");
        }
```
