Audit Report

## Title
Rate Limiter Keyed by Ephemeral `SessionId` (`PeerIndex`) Allows Unlimited Message Rate via Reconnection — (`network/src/protocols/hole_punching/mod.rs`)

## Summary

The `HolePunching` protocol's primary `rate_limiter` is keyed by `(PeerIndex, u32)`, where `PeerIndex` is a type alias for `SessionId` — an ephemeral, per-connection integer. Because each new TCP connection receives a fresh `SessionId`, an attacker can reset their rate limit counter at will by disconnecting and reconnecting, bypassing the intended 30 msg/sec cap entirely. Each bypassed message can trigger a gossip broadcast to `sqrt(total_peers)` other nodes, enabling sustained network-wide message amplification at negligible cost.

## Finding Description

`PeerIndex` is confirmed as a type alias for `SessionId` in `network/src/protocols/mod.rs`: [1](#0-0) 

The `HolePunching` struct holds two rate limiters with different key types: [2](#0-1) 

- `rate_limiter`: keyed by `(PeerIndex, u32)` — ephemeral session ID, resets on reconnect
- `forward_rate_limiter`: keyed by `(PeerId, PeerId, u32)` — stable peer identity (correct design)

In `received()`, the primary rate limit check uses the ephemeral `session_id`: [3](#0-2) 

When the rate limit is exceeded, the handler silently returns — **no ban is applied, no disconnect is triggered**. The `disconnected()` handler only calls `retain_recent()` (general cleanup) and does not remove the specific peer's entry; this is irrelevant anyway since the new connection gets a fresh key: [4](#0-3) 

The `accept_peer` function in `PeerRegistry` checks `PeerIdExists` to prevent duplicate simultaneous connections, but after a disconnect the peer is removed from the registry, so immediate reconnection is permitted: [5](#0-4) 

The ban check in `accept_peer` only blocks addresses that were explicitly banned via `ban_session` — which is never called on rate-limit-exceeded: [6](#0-5) 

When a `ConnectionRequest` passes the primary rate limiter and the target peer is not directly connected, `forward_message` gossip-broadcasts to `sqrt(total_peers)` other nodes: [7](#0-6) 

The secondary `forward_rate_limiter` (keyed by `(from, to, item_id)`) provides partial mitigation for forwarding, but is checked inside `execute()` after the primary limiter: [8](#0-7) 

Since the attacker controls the message payload, they can vary the `from`/`to` fields across reconnects to bypass the `forward_rate_limiter` as well.

## Impact Explanation

This matches the allowed CKB bounty impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

Each reconnect cycle resets the 30 msg/sec cap. With N reconnects per second, the effective inbound rate at the victim node is `N × 30` messages/sec, unbounded. Each message that reaches `forward_message` fans out to `sqrt(peers)` additional nodes, creating network-wide amplification. The `forward_rate_limiter` is bypassable by varying `from`/`to` fields, so it does not cap the amplification.

## Likelihood Explanation

The attack requires only a standard P2P client capable of rapid connect/disconnect cycles — no privileged access, no PoW, no key material. The TCP + secio handshake overhead limits raw reconnect frequency but does not prevent the bypass. Even one reconnect per second yields the full 30 msg/sec cap reset; faster reconnects multiply the effective rate proportionally. The attack is repeatable indefinitely and requires no special knowledge of the target network.

## Recommendation

Key the primary `rate_limiter` by `(PeerId, u32)` instead of `(PeerIndex, u32)`, consistent with how `forward_rate_limiter` is already keyed. The `PeerId` is available from `context.session.address` via `extract_peer_id` (already imported in `mod.rs`). Additionally, apply a short ban or exponential backoff when the rate limit is exceeded, rather than silently dropping the message, to prevent rapid reconnect cycling.

## Proof of Concept

```
1. Attacker opens TCP connection to victim → assigned SessionId=1
2. Attacker sends 30 valid ConnectionRequest messages (varying from/to to bypass forward_rate_limiter)
   → rate_limiter[(1, item_id)] exhausted; each message triggers forward_message → sqrt(peers) broadcast
3. Attacker disconnects
4. Attacker reconnects → assigned SessionId=2 (fresh key in HashMapStateStore)
5. Attacker sends 30 more messages → rate_limiter[(2, item_id)] starts fresh
6. Repeat: N reconnects × 30 messages = N×30 messages processed at victim,
   each fanning out to sqrt(peers) nodes network-wide
```

Assert: total messages processed per second at victim = `reconnect_rate × 30`, unbounded by the intended cap. Network-wide amplification = `reconnect_rate × 30 × sqrt(peers)`.

### Citations

**File:** network/src/protocols/mod.rs (L33-33)
```rust
pub type PeerIndex = SessionId;
```

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

**File:** network/src/peer_registry.rs (L97-99)
```rust
        if self.get_key_by_peer_id(&peer_id).is_some() {
            return Err(PeerError::PeerIdExists(peer_id).into());
        }
```

**File:** network/src/peer_registry.rs (L109-111)
```rust
            if peer_store.is_addr_banned(&remote_addr) {
                return Err(PeerError::Banned.into());
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
