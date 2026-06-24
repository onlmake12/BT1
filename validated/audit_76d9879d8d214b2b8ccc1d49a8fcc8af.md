All six cited code references are confirmed against the actual repository. Let me verify each claim precisely:

**Claim 1 — `ping_received()` unconditionally updates timestamp:** [1](#0-0) 
Confirmed. No rate limiting, no nonce check, no guard against unsolicited inbound Pings.

**Claim 2 — `sort_then_drop` removes the last `n` elements (protects them):** [2](#0-1) 
Confirmed. `truncate(list.len() - n)` keeps the first `list.len() - n` elements and drops the last `n` — those `n` peers sorted to the back are removed from the candidate pool (i.e., protected from eviction).

**Claim 3 — Round 2 sorts descending by duration, protecting peers with duration ≈ 0:** [3](#0-2) 
Confirmed. `peer2_last_message.cmp(&peer1_last_message)` is descending. Peers with `last_ping_protocol_message_received_at` just set (duration ≈ 0) sort to the back and are protected.

**Claim 4 — Round 1 maps `ping_rtt = None` to `u64::MAX`, leaving attacker peers as candidates:** [4](#0-3) 
Confirmed. `peer2_ping.cmp(&peer1_ping)` is descending; `u64::MAX` sorts to the front, so `None`-RTT peers are NOT in the protected tail and remain as eviction candidates — surviving to Round 2 where they get protected.

**Claim 5 — Design intent comment:** [5](#0-4) 
Confirmed. The comment explicitly states protection should be based on characteristics "hard to simulate or manipulate," which `last_ping_protocol_message_received_at` updated by inbound Pings violates.

**Claim 6 — `Peer::new` initializes `last_ping_protocol_message_received_at: None`:** [6](#0-5) 
Confirmed. Legitimate peers that have never sent a Ping map to `u64::MAX` duration in Round 2 and are not protected.

All claims are accurate. The exploit path is fully reachable by an unprivileged external peer. The impact matches the allowed bounty class "bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points).

---

Audit Report

## Title
Attacker-Controlled `last_ping_protocol_message_received_at` via Unsolicited Ping Flooding Bypasses Eviction Protection — (`network/src/protocols/ping.rs`, `network/src/peer_registry.rs`)

## Summary
`ping_received()` unconditionally updates `last_ping_protocol_message_received_at` on any inbound Ping message with no rate limiting. The eviction algorithm in `try_evict_inbound_peer()` uses this field to protect the 8 most recently active peers from eviction. An attacker holding 8+ inbound connections can flood Ping messages to permanently occupy all 8 "recent activity" protection slots in Round 2, causing legitimate peers with stale or `None` timestamps to be preferentially evicted and degrading the victim node's honest peer connectivity.

## Finding Description
**Root cause:** `ping_received()` (`ping.rs:62-69`) sets `peer.last_ping_protocol_message_received_at = Some(Instant::now())` for every inbound `PingPayload::Ping` message, with no rate limiting, no nonce validation, and no check that the local node initiated the exchange.

**Eviction algorithm flow** (`peer_registry.rs:142-211`):

- **Round 1 (ping RTT, lines 151-165):** Sorts candidates descending by `ping_rtt` (None → `u64::MAX`). Attacker peers that never respond to Pong have `ping_rtt = None` → mapped to `u64::MAX` → sorted to the front → **not** in the protected tail → remain as candidates.
- **Round 2 (recent activity, lines 167-183):** Sorts candidates descending by `last_ping_protocol_message_received_at` duration (None → `u64::MAX`). Attacker peers flooding Pings have duration ≈ 0 → sorted to the back → `truncate(list.len() - EVICTION_PROTECT_PEERS)` removes them from the candidate pool → **protected**. Legitimate peers with `None` or stale timestamps remain as candidates.
- **Round 3 (connection time, lines 185-188):** Protects half of remaining candidates by longest connection time.
- **Final eviction:** A peer is randomly chosen from the largest network group among remaining candidates — which are now disproportionately legitimate peers.

**Why existing checks fail:** The Pong handler (`ping.rs:71-79`) updates `ping_rtt` only after a completed round trip, but `ping_received()` has no such guard. There is no per-session Ping rate limit anywhere in the handler. The `sort_then_drop` helper correctly implements the protection logic, but the input field it relies on is fully attacker-controlled.

## Impact Explanation
This is a **bad design which could cause CKB network congestion with few costs** (High, 10001–15000 points). An attacker with 8 inbound connections can guarantee those connections are never evicted by Round 2, while legitimate peers are systematically displaced. Over repeated eviction cycles, the attacker accumulates a disproportionate share of the victim node's inbound slots. A node with degraded honest peer connectivity propagates blocks and transactions less efficiently, contributing to network-wide relay degradation and potential congestion. The attack is persistent and self-reinforcing: any attacker peer that is evicted can immediately reconnect and resume Ping flooding.

## Likelihood Explanation
The exploit requires only: (1) the ability to open 8 inbound TCP connections to the target node — no proof-of-work, no key, no privilege; (2) sending `PingMessage::build_ping(any_nonce)` in a loop using the standard CKB ping protocol format. The code path through `received()` → `ping_received()` is fully reachable from the P2P network layer. There is no rate limiting, no connection-level Ping quota, and no server-side nonce requirement for inbound Pings. The attack is repeatable indefinitely.

## Recommendation
1. **Move the timestamp update out of `ping_received()` and keep it only in `pong_received()`** (`ping.rs:71-79`). The field should reflect genuine bidirectional protocol participation (a completed Ping/Pong round trip), not merely receipt of an unsolicited Ping. This makes the field hard to manipulate without also completing the RTT exchange, aligning it with the design comment at `peer_registry.rs:149`.
2. **Add per-session inbound Ping rate limiting** in `PingHandler` (e.g., track last inbound Ping time per `SessionId` in `connected_session_ids` and ignore or disconnect peers that exceed one Ping per interval window).
3. Consider renaming `last_ping_protocol_message_received_at` to `last_pong_received_at` to make the intended semantics explicit and prevent future regressions.

## Proof of Concept
```
1. Attacker opens 8 inbound TCP connections to victim node.
2. Each attacker session sends PingMessage::build_ping(any_nonce) in a tight loop.
3. Each Ping triggers ping_received() → last_ping_protocol_message_received_at = Instant::now().
   Attacker peers never respond to Pong → ping_rtt remains None.
4. Legitimate peer (session N+1) attempts to connect → max_inbound reached →
   try_evict_inbound_peer() is called.
5. Round 1: attacker peers have ping_rtt=None → u64::MAX → sorted to front →
   NOT protected, remain as candidates.
6. Round 2: attacker peers have last_ping_protocol_message_received_at duration ≈ 0 →
   sorted to back → truncate removes them → PROTECTED, removed from candidates.
7. Remaining candidates: legitimate peers with None or stale timestamps.
8. Round 3 + random eviction: a legitimate peer is chosen and evicted.
9. Attacker reconnects any evicted peer immediately and resumes Ping flooding.
10. Invariant to assert: after ≥8 attacker peers are active and flooding,
    no attacker peer is ever selected as the final eviction target.
```
Minimal unit test: populate a `PeerRegistry` with 8 attacker `Peer` entries (all `ping_rtt=None`, `last_ping_protocol_message_received_at=Some(Instant::now())`) and N legitimate entries (`last_ping_protocol_message_received_at=None`), call `try_evict_inbound_peer()` repeatedly, and assert the returned `SessionId` is never one of the 8 attacker sessions.

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

**File:** network/src/peer_registry.rs (L149-150)
```rust
        // Protect peers based on characteristics that an attacker hard to simulate or manipulate
        // Protect peers which has the lowest ping
```

**File:** network/src/peer_registry.rs (L151-165)
```rust
        sort_then_drop(
            &mut candidate_peers,
            EVICTION_PROTECT_PEERS,
            |peer1, peer2| {
                let peer1_ping = peer1
                    .ping_rtt
                    .map(|p| p.as_secs())
                    .unwrap_or_else(|| u64::MAX);
                let peer2_ping = peer2
                    .ping_rtt
                    .map(|p| p.as_secs())
                    .unwrap_or_else(|| u64::MAX);
                peer2_ping.cmp(&peer1_ping)
            },
        );
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

**File:** network/src/peer.rs (L103-103)
```rust
            last_ping_protocol_message_received_at: None,
```
