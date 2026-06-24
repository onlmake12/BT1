Audit Report

## Title
Attacker-Controlled `last_ping_protocol_message_received_at` Bypasses Inbound Peer Eviction Protection — (`network/src/protocols/ping.rs`, `network/src/peer_registry.rs`)

## Summary
`ping_received()` unconditionally writes `Instant::now()` into `last_ping_protocol_message_received_at` on every incoming unsolicited Ping, with no rate limit or authentication. `try_evict_inbound_peer()` uses this same field to protect the 8 most recently active peers from eviction. An attacker who floods the node with Ping messages keeps their elapsed time at ≈ 0 seconds and is permanently shielded from eviction, allowing them to monopolize inbound peer slots and displace honest peers.

## Finding Description

**Root cause — `ping_received()` (`ping.rs` lines 62–69):**

Every incoming `PingPayload::Ping` unconditionally calls `ping_received()`, which writes `Instant::now()` into the peer's registry entry with no guard, rate limit, or nonce validation:

```rust
fn ping_received(&mut self, id: SessionId) {
    self.network_state.with_peer_registry_mut(|reg| {
        if let Some(peer) = reg.get_peer_mut(id) {
            peer.last_ping_protocol_message_received_at = Some(Instant::now());
        }
    });
}
``` [1](#0-0) 

This is triggered from the `received()` handler at: [2](#0-1) 

**Eviction logic — `try_evict_inbound_peer()` (`peer_registry.rs` lines 167–183):**

The second protection round sorts remaining candidates by `last_ping_protocol_message_received_at` elapsed time (descending) and removes the 8 with the smallest elapsed duration (most recent activity) from the eviction pool: [3](#0-2) 

`sort_then_drop` sorts ascending and truncates the front, keeping the tail — i.e., the peers with elapsed ≈ 0 are always kept: [4](#0-3) 

**Why round 1 does not protect legitimate peers:**

Round 1 protects 8 peers with the lowest `ping_rtt`. The attacker deliberately ignores the server's Ping (never sends a valid Pong), so `ping_rtt` stays `None` → `u64::MAX`. The attacker is NOT protected in round 1 and remains a candidate — but is then shielded in round 2 via the manipulated timestamp. `ping_rtt` is only set in `pong_received()`, which requires a valid nonce-matched Pong: [5](#0-4) 

**Attacker-writable field confirmed:** [6](#0-5) 

**Exploit flow:**
1. Attacker fills `max_inbound` slots with controlled peers.
2. Each attacker peer sends Ping messages in a tight loop → `ping_received()` → `last_ping_protocol_message_received_at = Instant::now()`.
3. When a legitimate peer connects, `accept_peer()` calls `try_evict_inbound_peer()`.
4. Round 2 of `sort_then_drop` protects the 8 most recently active peers. All attacker peers have elapsed ≈ 0 s; legitimate peers have elapsed > 0 s.
5. Attacker peers are always in the protected tail; a legitimate peer is evicted instead. [7](#0-6) 

## Impact Explanation
An attacker with multiple inbound connections can permanently monopolize all `EVICTION_PROTECT_PEERS` (8) "recently active" slots, ensuring that honest peers are always selected for eviction instead. This systematically degrades the node's inbound peer diversity and network topology. Scaled across multiple nodes, this constitutes a low-cost mechanism to degrade CKB network connectivity — matching **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
The attack requires only a standard TCP connection to the node's P2P port and the ability to send well-formed Ping messages in a loop. No cryptographic material, hashpower, or privileged access is needed. The Ping message format is public. There is no rate limiting, no per-session message budget, and no cost imposed on the sender. The attack is trivially repeatable and can be sustained indefinitely.

## Recommendation
1. **Only update `last_ping_protocol_message_received_at` on a valid authenticated Pong** (matching nonce), not on an unsolicited incoming Ping. `pong_received()` already does this correctly; `ping_received()` should not touch this field.
2. **Rate-limit incoming Ping messages** per session (e.g., one per interval) to prevent flooding.
3. **Rename/separate the field** to make the semantic explicit: "last genuine protocol activity" should only be set on authenticated round-trips.

## Proof of Concept
```
1. Connect to a CKB node as an inbound peer (standard P2P handshake).
2. Fill max_inbound slots with attacker-controlled peers.
3. Each attacker peer sends a Ping message every ~1 ms in a tight loop.
4. Each Ping triggers ping_received() → last_ping_protocol_message_received_at = Instant::now().
5. Attacker peers deliberately do NOT respond to the server's Ping (so ping_rtt stays None).
6. When a legitimate peer attempts to connect, try_evict_inbound_peer() is called.
7. Round 1: attacker peers have ping_rtt = None → u64::MAX, so they are NOT protected.
8. Round 2: attacker peers have elapsed ≈ 0 s; legitimate peers have elapsed > 0 s.
   sort_then_drop keeps the 8 most recent → all attacker peers are protected.
9. A legitimate peer is selected for eviction; no attacker peer is ever disconnected.
10. Assert: legitimate peer receives PeerError::ReachMaxInboundLimit or is immediately
    evicted, while all attacker peers remain connected indefinitely.
```

### Citations

**File:** network/src/protocols/ping.rs (L62-69)
```rust
    fn ping_received(&mut self, id: SessionId) {
        trace!("received ping from: {:?}", id);
        self.network_state.with_peer_registry_mut(|reg| {
            if let Some(peer) = reg.get_peer_mut(id) {
                peer.last_ping_protocol_message_received_at = Some(Instant::now());
            }
        });
    }
```

**File:** network/src/protocols/ping.rs (L71-79)
```rust
    fn pong_received(&mut self, id: SessionId, last_ping: Instant) {
        let now = Instant::now();
        self.network_state.with_peer_registry_mut(|reg| {
            if let Some(peer) = reg.get_peer_mut(id) {
                peer.ping_rtt = Some(now.saturating_duration_since(last_ping));
                peer.last_ping_protocol_message_received_at = Some(now);
            }
        });
    }
```

**File:** network/src/protocols/ping.rs (L215-216)
```rust
                    PingPayload::Ping(nonce) => {
                        self.ping_received(session.id);
```

**File:** network/src/peer_registry.rs (L55-63)
```rust
fn sort_then_drop<T, F>(list: &mut Vec<T>, n: usize, compare: F)
where
    F: FnMut(&T, &T) -> std::cmp::Ordering,
{
    list.sort_by(compare);
    if list.len() > n {
        list.truncate(list.len() - n);
    }
}
```

**File:** network/src/peer_registry.rs (L116-121)
```rust
                if connection_status.non_whitelist_inbound >= self.max_inbound {
                    if let Some(evicted_session) = self.try_evict_inbound_peer(peer_store) {
                        evicted_peer = self.remove_peer(evicted_session);
                    } else {
                        return Err(PeerError::ReachMaxInboundLimit.into());
                    }
```

**File:** network/src/peer_registry.rs (L167-183)
```rust
        // Protect peers which most recently sent messages
        sort_then_drop(
            &mut candidate_peers,
            EVICTION_PROTECT_PEERS,
            |peer1, peer2| {
                let now = Instant::now();
                let peer1_last_message = peer1
                    .last_ping_protocol_message_received_at
                    .map(|t| now.saturating_duration_since(t).as_secs())
                    .unwrap_or_else(|| u64::MAX);
                let peer2_last_message = peer2
                    .last_ping_protocol_message_received_at
                    .map(|t| now.saturating_duration_since(t).as_secs())
                    .unwrap_or_else(|| u64::MAX);
                peer2_last_message.cmp(&peer1_last_message)
            },
        );
```

**File:** network/src/peer.rs (L70-71)
```rust
    /// Ping/Pong message last received time
    pub last_ping_protocol_message_received_at: Option<Instant>,
```
