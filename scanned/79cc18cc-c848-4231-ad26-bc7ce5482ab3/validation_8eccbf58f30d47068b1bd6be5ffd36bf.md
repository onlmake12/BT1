All cited code paths are confirmed against the actual source. Here is the validation result:

---

Audit Report

## Title
Unsolicited Ping Flood Manipulates Eviction Protection to Permanently Occupy Inbound Slot — (`network/src/protocols/ping.rs`, `network/src/peer_registry.rs`)

## Summary
`ping_received()` unconditionally writes `Instant::now()` to `last_ping_protocol_message_received_at` for every inbound `Ping` message, with no rate-limit and no requirement that the node first sent a `Ping`. The second protection pass in `try_evict_inbound_peer()` shields the 8 peers whose `last_ping_protocol_message_received_at` is most recent from eviction. An attacker who floods unsolicited `Ping` messages always holds the freshest timestamp and is therefore always protected, permanently occupying an inbound slot.

## Finding Description

**Timestamp written on every inbound Ping — no solicitation guard:**

`ping_received()` at `network/src/protocols/ping.rs` lines 62–69 writes `Instant::now()` to the peer's registry entry unconditionally:

```rust
fn ping_received(&mut self, id: SessionId) {
    self.network_state.with_peer_registry_mut(|reg| {
        if let Some(peer) = reg.get_peer_mut(id) {
            peer.last_ping_protocol_message_received_at = Some(Instant::now());
        }
    });
}
```

It is called at line 216 for every decoded `PingPayload::Ping`, with no check that the node ever sent a `Ping` first, no rate-limit, and no counter.

**Eviction protection reads that same field:**

`try_evict_inbound_peer()` in `network/src/peer_registry.rs` lines 167–183 runs a `sort_then_drop` pass that sorts candidates by `last_ping_protocol_message_received_at` elapsed time (descending, i.e., oldest first) and then calls `truncate(list.len() - EVICTION_PROTECT_PEERS)`, which removes the oldest candidates and keeps the 8 most recently active in the protected set (removed from the eviction pool).

**`sort_then_drop` mechanics confirmed:**

`sort_then_drop` (lines 55–63) sorts ascending by the supplied comparator and then truncates the tail by `n`. The comparator for the second pass is `peer2_last_message.cmp(&peer1_last_message)` — a reverse comparison — so peers with the *smallest* elapsed time (most recent) end up at the tail and are truncated out of the candidate list (i.e., protected).

**Timeout path never fires for the attacker:**

`CHECK_TIMEOUT_TOKEN` (lines 254–268) disconnects peers only when `ps.processing && ps.elapsed() >= timeout`. `processing` is set to `true` exclusively in `ping_peers()` (line 91) when the *node* sends an outbound `Ping`. Receiving inbound `Ping` messages never sets `processing = true`, so the attacker is never disconnected by the timeout path.

**First protection pass does not protect the attacker:**

`ping_rtt` is set only in `pong_received()` (line 75), not in `ping_received()`. An attacker sending only `Ping` messages has `ping_rtt = None`, which maps to `u64::MAX` in the first sort (lines 151–165), placing them at the front (worst RTT). They are not protected by the first pass and remain in the candidate pool until the second pass protects them via the timestamp flood.

**Why the attacker always wins the second pass:**

The node sends outbound `Ping` messages at a fixed `interval` (typically 15 s). Legitimate peers update `last_ping_protocol_message_received_at` only when they respond with a `Pong` (via `pong_received()`). The attacker can send `Ping` messages at any rate (e.g., every millisecond), so their timestamp is always fresher than any legitimate peer's, guaranteeing a place in the top-8 protected set.

**Slot is permanently held:**

When a new legitimate peer attempts to connect and `accept_peer()` calls `try_evict_inbound_peer()` (lines 115–121), the attacker is always in the protected set and is never selected for eviction. The legitimate peer receives `PeerError::ReachMaxInboundLimit` if no other unprotected candidate exists.

## Impact Explanation

Up to 8 coordinated attackers (matching `EVICTION_PROTECT_PEERS`) can each permanently hold one inbound slot. Each attacker requires only a standard P2P connection and the ability to send `Ping` messages in a loop. With 8 attackers, 8 inbound slots are permanently occupied and cannot be reclaimed by legitimate peers. On a node with a small `max_inbound` (e.g., 8–32), this constitutes a near-complete eclipse of inbound connections, causing network congestion and degrading peer diversity at negligible cost.

This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10001–15000 points).**

## Likelihood Explanation

The attack requires only a valid P2P connection and the ability to send `Ping` messages (a 4-byte nonce, public format). No cryptographic material, hashpower, or privileged access is needed. The attack is repeatable, persistent, and trivially automated. Any node on the network can execute it.

## Recommendation

1. **Only update `last_ping_protocol_message_received_at` in `pong_received()`**, not in `ping_received()`. A `Pong` response must echo the correct nonce from a node-initiated `Ping`, making it impossible to forge without first receiving the node's outbound `Ping`.
2. **Rate-limit inbound `Ping` messages** per session (e.g., one per interval window) to prevent flooding regardless of the timestamp update logic.
3. Optionally rename the field to `last_pong_received_at` to make the intended semantics explicit and prevent future regressions.

## Proof of Concept

```
1. Node N has max_inbound = K (all slots filled with legitimate peers L_1..L_K).
2. Attacker A connects as inbound peer; one legitimate peer is evicted to make room.
3. A enters a tight loop: send Ping(nonce=any) → node replies Pong → repeat at high frequency.
   (No nonce validation on receipt; the node replies to any nonce.)
4. A's last_ping_protocol_message_received_at is refreshed to Instant::now() on every iteration,
   always fresher than any legitimate peer's timestamp (updated only every ~15 s via Pong).
5. A new legitimate peer L tries to connect → accept_peer() calls try_evict_inbound_peer().
6. Second sort_then_drop protects the 8 most recently active peers; A is always in that set.
7. A is never selected for eviction; L receives ReachMaxInboundLimit.

Invariant test:
- Fill max_inbound slots with peers all flooding Pings.
- Assert try_evict_inbound_peer() returns None (no evictable candidate remains).
- Confirm accept_peer() returns Err(PeerError::ReachMaxInboundLimit) for every new connection attempt.
```