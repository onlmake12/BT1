Audit Report

## Title
Rate Limiter Keyed by Ephemeral `SessionId` Instead of Stable `PeerId` Allows Rate Limit Bypass via Reconnection — (`network/src/protocols/hole_punching/mod.rs`)

## Summary

The `HolePunching` protocol's primary `rate_limiter` is keyed by `(PeerIndex, u32)` where `PeerIndex` is a type alias for `SessionId` — an ephemeral, per-connection integer. An attacker can reset their rate limit counter at will by disconnecting and reconnecting, since each new TCP connection receives a new `SessionId`. Each bypassed `ConnectionRequest` that cannot be directly routed triggers a `filter_broadcast` to `sqrt(total_peers)` other nodes, creating a message amplification effect with no ban enforcement on rate-limit violations.

## Finding Description

`PeerIndex` is confirmed as a type alias for `SessionId` at `network/src/protocols/mod.rs:33`: [1](#0-0) 

The `HolePunching` struct holds two rate limiters with different key types: [2](#0-1) 

- `rate_limiter`: keyed by `(PeerIndex, u32)` = `(SessionId, u32)` — ephemeral per-connection integer
- `forward_rate_limiter`: keyed by `(PeerId, PeerId, u32)` — stable cryptographic identity

In `received()`, the primary rate limit check uses the ephemeral `session_id`: [3](#0-2) 

When the rate limit is exceeded, the handler silently returns — **no ban is applied**. The `disconnected()` handler only calls `retain_recent()` (general cleanup), which is irrelevant since the new connection gets a fresh key anyway: [4](#0-3) 

Each `ConnectionRequest` that passes the rate limiter and cannot be directly routed calls `forward_message()`, which performs a `filter_broadcast` to `sqrt(total_peers)` other nodes: [5](#0-4) 

The `forward_rate_limiter` is checked before forwarding, keyed by `(from, to, item_id)`: [6](#0-5) 

However, the attacker controls the `from` and `to` fields in the message payload, so varying these fields across messages bypasses the `forward_rate_limiter` as well.

The `accept_peer` ban check in `PeerRegistry` only blocks addresses that were explicitly banned: [7](#0-6) 

Since rate-limit-exceeded never triggers `ban_session`, the attacker's address is never added to the ban list, and immediate reconnection is always permitted after disconnect.

## Impact Explanation

This matches the allowed CKB bounty impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**.

An attacker sustains an arbitrarily high inbound message rate at the victim node. Each accepted `ConnectionRequest` fans out to `sqrt(peers)` other nodes via `filter_broadcast`, creating a network-wide amplification effect. With N reconnects per second, the effective message rate is `N × 30`, unbounded by the intended 30 msg/sec cap. This can cause sustained congestion across the P2P network.

## Likelihood Explanation

The attack requires only a standard P2P client capable of rapid connect/disconnect cycles. No privileged access, no proof-of-work, and no key material is needed. The TCP + secio handshake overhead limits raw reconnect frequency but does not prevent the bypass — even one reconnect per second yields the full 30 msg/sec cap reset. The attack is repeatable indefinitely and requires minimal resources.

## Recommendation

Key the primary `rate_limiter` by `(PeerId, u32)` instead of `(PeerIndex, u32)`, consistent with how `forward_rate_limiter` is already keyed. The `PeerId` is available from `context.session.address` via `extract_peer_id`. Additionally, apply a short ban or exponential backoff when the rate limit is exceeded, rather than silently dropping the message, to prevent the reconnect-reset cycle.

## Proof of Concept

```
1. Attacker opens TCP connection to victim → assigned SessionId=1
2. Attacker sends 30 valid ConnectionRequest messages (varying from/to fields)
   → rate_limiter[(1, item_id)] exhausted; forward_rate_limiter bypassed by varying from/to
3. Attacker disconnects
4. Attacker reconnects → assigned SessionId=2 (fresh key in HashMapStateStore)
5. Attacker sends 30 more messages → rate_limiter[(2, item_id)] starts fresh
6. Each forwarded message triggers filter_broadcast to sqrt(peers) nodes
7. Repeat: N reconnects × 30 messages = N×30 messages processed per second,
   each fanning out to sqrt(peers) nodes — total network messages = N×30×sqrt(peers)
```

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

**File:** network/src/peer_registry.rs (L109-111)
```rust
            if peer_store.is_addr_banned(&remote_addr) {
                return Err(PeerError::Banned.into());
            }
```
