### Title
Rate Limiter Keyed by Ephemeral `PeerIndex` Allows Unlimited Hole Punching Message Flood via Reconnect Cycling — (`network/src/protocols/hole_punching/mod.rs`)

---

### Summary

The `HolePunching` protocol's primary `rate_limiter` is keyed by `(PeerIndex, u32)` where `PeerIndex` is an ephemeral session ID (`SessionId`) assigned fresh on every TCP connection. Because each reconnect yields a new `PeerIndex`, an attacker can trivially reset their rate limit counter by disconnecting and reconnecting, bypassing the intended 30 msg/sec cap entirely.

---

### Finding Description

In `network/src/protocols/hole_punching/mod.rs`, the `HolePunching` struct declares:

```rust
rate_limiter: RateLimiter<(PeerIndex, u32)>,
``` [1](#0-0) 

`PeerIndex` is a type alias for `SessionId` from the tentacle library:

```rust
pub type PeerIndex = SessionId;
``` [2](#0-1) 

In `received()`, the rate check uses the ephemeral session ID:

```rust
if self.rate_limiter.check_key(&(session_id, msg.item_id())).is_err() {
    ...
    return;
}
``` [3](#0-2) 

The `disconnected()` handler only calls `retain_recent()` — a memory-management GC operation — and does **not** remove or invalidate the disconnected session's rate limit state:

```rust
async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
    self.rate_limiter.retain_recent();
    self.forward_rate_limiter.retain_recent();
    ...
}
``` [4](#0-3) 

When a peer reconnects, `accept_peer` assigns a brand-new `SessionId` (tentacle's session IDs are monotonically increasing integers). The new key `(new_session_id, msg_item_id)` has no prior rate limit history, so the 30 msg/sec window starts fresh. [5](#0-4) 

The comment in `new()` confirms the intent was per-peer limiting, but the implementation uses a per-session key:

```rust
// setup a rate limiter keyed by peer and message type that lets through 30 requests per second
let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
``` [6](#0-5) 

The `forward_rate_limiter` is correctly keyed by `(PeerId, PeerId, u32)` using identity-stable peer IDs from message content, but the primary `rate_limiter` — the first and only guard before message dispatch — is not. [7](#0-6) 

---

### Impact Explanation

Each `ConnectionRequest` that passes the rate limiter can trigger a gossip broadcast to `sqrt(total_peers)` other nodes. By cycling connections, an attacker sustains an unbounded message rate, causing:

- Unbounded CPU consumption on the victim node (deserialization, peer registry lookups, gossip fan-out)
- Network amplification: each attacker message fans out to `sqrt(N)` peers
- Effective DoS of the hole punching protocol and associated processing pipeline

---

### Likelihood Explanation

The attack requires only a standard inbound P2P connection — no special privileges, no leaked keys, no majority hashpower. The attacker needs to implement a simple connect → send 30 messages → disconnect → reconnect loop. The secio handshake adds per-reconnect overhead but does not prevent the attack at any meaningful scale.

---

### Recommendation

Key the `rate_limiter` by `(PeerId, u32)` instead of `(PeerIndex, u32)`. The `PeerId` is stable across reconnects and is available via `extract_peer_id(&context.session.address)` at the point of the `received()` call. This matches the design already used by `forward_rate_limiter`.

---

### Proof of Concept

```
loop:
  connect to victim (new PeerIndex N assigned)
  send 30 ConnectionRequest messages  → all pass rate limiter for key (N, 0)
  disconnect
  reconnect (new PeerIndex N+1)
  send 30 more ConnectionRequest messages → all pass rate limiter for key (N+1, 0)
  ...
```

Total messages processed per second = `30 × reconnect_frequency`, unbounded. Each message may fan out to `sqrt(connected_peers)` additional nodes via gossip broadcast.

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

**File:** network/src/protocols/hole_punching/mod.rs (L249-252)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/mod.rs (L33-33)
```rust
pub type PeerIndex = SessionId;
```

**File:** network/src/peer_registry.rs (L86-139)
```rust
    pub(crate) fn accept_peer(
        &mut self,
        remote_addr: Multiaddr,
        session_id: SessionId,
        raw_session_type: RawSessionType,
        peer_store: &mut PeerStore,
    ) -> Result<Option<Peer>, Error> {
        if self.peers.contains_key(&session_id) {
            return Err(PeerError::SessionExists(session_id).into());
        }
        let peer_id = extract_peer_id(&remote_addr).expect("opened session should have peer id");
        if self.get_key_by_peer_id(&peer_id).is_some() {
            return Err(PeerError::PeerIdExists(peer_id).into());
        }

        let is_whitelist = self.whitelist_peers.contains(&peer_id);
        let mut evicted_peer: Option<Peer> = None;

        let mut session_type: SessionType = raw_session_type.into();
        if !is_whitelist {
            if self.whitelist_only {
                return Err(PeerError::NonReserved.into());
            }
            if peer_store.is_addr_banned(&remote_addr) {
                return Err(PeerError::Banned.into());
            }

            let connection_status = self.connection_status();
            // check peers connection limitation
            if session_type.is_inbound() {
                if connection_status.non_whitelist_inbound >= self.max_inbound {
                    if let Some(evicted_session) = self.try_evict_inbound_peer(peer_store) {
                        evicted_peer = self.remove_peer(evicted_session);
                    } else {
                        return Err(PeerError::ReachMaxInboundLimit.into());
                    }
                }
            } else if connection_status.non_whitelist_outbound >= self.max_outbound {
                if self.disable_block_relay_only_connection
                    || connection_status.block_relay_only_outbound_count
                        >= self.max_outbound_block_relay
                {
                    return Err(PeerError::ReachMaxOutboundLimit.into());
                } else {
                    peer_store.add_anchors(remote_addr.clone());
                    session_type = SessionType::BlockRelayOnly;
                }
            }
        }
        peer_store.add_connected_peer(remote_addr.clone(), session_type);
        let peer = Peer::new(session_id, session_type, remote_addr, is_whitelist);
        self.peers.insert(session_id, peer);
        Ok(evicted_peer)
    }
```
