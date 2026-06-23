The vulnerability is real and concrete. Here is the full analysis:

---

### Title
Rate Limiter Keyed by Ephemeral `PeerIndex` (Session ID) Instead of Stable `PeerId` Allows Unlimited Message Rate via Reconnection — (`network/src/protocols/hole_punching/mod.rs`)

### Summary

The `HolePunching` protocol's primary `rate_limiter` is keyed by `(PeerIndex, u32)` where `PeerIndex` is an alias for `SessionId` — an ephemeral, per-connection integer. Each new TCP connection receives a new `SessionId`, so an attacker can reset their rate limit counter at will by disconnecting and reconnecting, bypassing the intended 30 msg/sec cap entirely.

### Finding Description

`PeerIndex` is defined as a type alias for `SessionId`: [1](#0-0) 

The `HolePunching` struct holds two rate limiters: [2](#0-1) 

- `rate_limiter`: keyed by `(PeerIndex, u32)` — **ephemeral session ID**
- `forward_rate_limiter`: keyed by `(PeerId, PeerId, u32)` — **stable peer identity** ✓

In `received()`, the primary rate limit check uses the ephemeral `session_id`: [3](#0-2) 

When the rate limit is exceeded, the handler simply returns — **no ban is applied**. The `disconnected()` handler only calls `retain_recent()` (a general cleanup), it does not remove the specific peer's entry nor does it matter since the new connection gets a fresh key: [4](#0-3) 

The `accept_peer` function in `PeerRegistry` checks for `PeerIdExists` (preventing duplicate simultaneous connections from the same peer), but after a disconnect the peer is removed from the registry, so immediate reconnection is permitted: [5](#0-4) 

The ban check in `accept_peer` only blocks addresses that were explicitly banned: [6](#0-5) 

Since rate-limit-exceeded never triggers `ban_session`, the attacker's address is never added to the ban list.

### Impact Explanation

An attacker executes the cycle: **connect → send 30 `ConnectionRequest` messages → disconnect → reconnect (new `PeerIndex`) → send 30 more**. Each `ConnectionRequest` that passes the rate limiter triggers `forward_message`, which gossip-broadcasts to `sqrt(total_peers)` other nodes: [7](#0-6) 

This creates a message amplification effect. The attacker sustains an arbitrarily high inbound message rate at the victim node, and each accepted message fans out to other peers, causing network-wide congestion. The `forward_rate_limiter` (keyed by `(from, to, item_id)`) provides partial mitigation for forwarding, but the attacker can vary the `from`/`to` fields in the message payload to bypass it as well.

### Likelihood Explanation

The attack requires only a standard P2P client capable of rapid connect/disconnect cycles. No privileged access, no PoW, no key material is needed. The TCP + secio handshake overhead limits raw reconnect frequency but does not prevent the bypass — even one reconnect per second yields the full 30 msg/sec cap reset, and faster reconnects multiply the effective rate proportionally.

### Recommendation

Key the primary `rate_limiter` by `(PeerId, u32)` instead of `(PeerIndex, u32)`, consistent with how `forward_rate_limiter` is already keyed. The `PeerId` is available from `context.session.address` via `extract_peer_id`. Additionally, consider applying a short ban or exponential backoff when the rate limit is exceeded, rather than silently dropping the message.

### Proof of Concept

```
1. Attacker opens TCP connection to victim → assigned SessionId=1
2. Attacker sends 30 valid ConnectionRequest messages → rate_limiter[(1, item_id)] exhausted
3. Attacker disconnects
4. Attacker reconnects → assigned SessionId=2 (fresh key in HashMapStateStore)
5. Attacker sends 30 more messages → rate_limiter[(2, item_id)] starts fresh
6. Repeat: N reconnects × 30 messages = N×30 messages processed, each fanning out to sqrt(peers) nodes
```

Assert: total messages processed per second at victim node = `reconnect_rate × 30`, unbounded by the intended cap.

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

**File:** network/src/protocols/hole_punching/mod.rs (L223-234)
```rust
                    // Broadcast to a number of nodes equal to the square root of the total connection count using gossip.
                    let mut total = status.total.isqrt();
                    let _ignore = context
                        .filter_broadcast(
                            TargetSession::Filter(Box::new(move |_| {
                                total = total.saturating_sub(1);
                                total != 0
                            })),
                            proto_id,
                            conn_req.as_bytes(),
                        )
                        .await;
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
