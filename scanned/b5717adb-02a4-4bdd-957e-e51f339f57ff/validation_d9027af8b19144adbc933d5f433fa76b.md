Audit Report

## Title
Unauthenticated Route-Bypass Message Injection via Missing Route Membership Check in `ConnectionRequestDeliveredProcess::execute` — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary

The `execute()` method in `ConnectionRequestDeliveredProcess` unconditionally forwards a `ConnectionRequestDelivered` message to any connected peer named in the attacker-controlled `content.from` field when `route` is empty, without verifying that the relay was ever a legitimate member of the original hole-punching route. This breaks the routing invariant and enables any unprivileged peer connected to a relay to inject fully attacker-crafted messages to any other peer connected to that relay. When the victim has an active `inflight_requests` entry, it is further induced to make repeated outbound TCP connection attempts to attacker-controlled endpoints (SSRF/port-scan primitive). The `forward_rate_limiter` is trivially bypassed by varying `content.to`, allowing the attacker to sustain the injection up to the session-level cap of 30 messages per second.

## Finding Description

**Root cause — missing route membership check (L147–153):**

In `execute()`, the dispatch on `content.route.last()` produces `None` when `route` is empty. The `None` branch checks only whether `local_peer_id == content.from`; if not (always true when the attacker sets `from` to a victim peer ID), it calls `forward_delivered(&content.from)` with no verification that the relay ever forwarded a `ConnectionRequest` for this `(from, to)` pair:

```rust
match content.route.last() {
    Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
    None => {
        let self_peer_id = self.protocol.network_state.local_peer_id();
        if self_peer_id != &content.from {
            self.forward_delivered(&content.from).await   // ← no route membership check
```

**Unconditional forwarding (L182–212):**

`forward_delivered` performs a `peer_registry` read-lock lookup for the attacker-supplied peer ID and, if connected, sends the full attacker-crafted message to it. No state is consulted to confirm the relay's prior participation in a legitimate route.

**Rate limiter bypass (L134–145):**

The `forward_rate_limiter` is keyed on `(content.from, content.to, msg_item_id)`. All three values are attacker-controlled. By varying `content.to` across requests, the attacker generates distinct keys and bypasses the 1-req/sec-per-key limit. The only remaining cap is the session-level `rate_limiter` at 30 req/sec per `(session_id, item_id)`.

**Victim-side SSRF (L160–176, component/mod.rs L49–115):**

When the victim receives the injected message (`route = []`, `from = victim_peer_id`), it enters the "target peer" branch and calls `inflight_requests.remove(&content.to)`. If a matching entry exists (populated routinely by the `notify` timer), it spawns `try_nat_traversal(ttl, content.listen_addrs)` — a 30-second loop making ~150 TCP connection attempts to fully attacker-controlled IP:port endpoints.

## Impact Explanation

**Allowed impact: High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

The relay-side injection is unconditional: any unprivileged peer connected to a relay can cause that relay to forward up to 30 crafted `ConnectionRequestDelivered` messages per second to any other peer connected to the same relay, with zero legitimate routing state required. By connecting to multiple relay nodes simultaneously, an attacker multiplies this rate linearly (30 × N relays). Each injected message consumes processing resources on the victim and, when `inflight_requests` entries exist, spawns long-lived async tasks that open TCP sockets to attacker-chosen endpoints — amplifying resource exhaustion on the victim node. The combination of unauthenticated relay-side forwarding, a bypassable rate limiter, and victim-side resource consumption constitutes a low-cost, scalable bad design that can cause CKB P2P network congestion and victim node resource exhaustion.

## Likelihood Explanation

**Relay-side injection:** Requires only that the attacker be a connected P2P peer (any unprivileged node) and know the peer ID of a victim also connected to the same relay. Peer IDs are publicly observable on the CKB P2P network. No special privileges, leaked keys, or victim mistakes are required.

**Victim-side SSRF escalation:** Requires additionally that the victim has an active `inflight_requests` entry for `content.to`. This is populated automatically whenever the victim's `notify` timer fires (every 5 minutes per `CHECK_INTERVAL`) and the victim has fewer outbound connections than `max_outbound` — a routine background condition for any under-connected node.

The attack is repeatable: after `inflight_requests` entries are consumed (via `remove`), the victim repopulates them at the next `notify` tick, restoring the SSRF surface.

## Recommendation

In the `None` (empty route) branch of `execute()`, before calling `forward_delivered(&content.from)`, verify that the local node was a legitimate relay for this `(from, to)` pair. Concretely:

1. Maintain a bounded set (e.g., `HashSet<(PeerId, PeerId)>`) of `(from, to)` pairs for which the node has previously forwarded a `ConnectionRequest` (populated in `ConnectionRequestProcess::execute`).
2. In the `None` branch of `ConnectionRequestDeliveredProcess::execute`, reject (return `StatusCode::Ignore`) any message whose `(content.from, content.to)` pair is not present in this set.
3. Expire entries from the set after a reasonable TTL (e.g., `TIMEOUT`) to bound memory usage.

Additionally, key the `forward_rate_limiter` on `(sender_session_id, content.from)` rather than `(content.from, content.to, item_id)` to prevent bypass via varying `content.to`.

## Proof of Concept

**Minimal manual steps:**

1. Attacker peer A connects to relay node R via the CKB P2P protocol.
2. Victim peer V is also connected to R (V's peer ID is observable from the P2P network).
3. A sends a `ConnectionRequestDelivered` message to R with:
   - `route = []` (empty)
   - `from = V.peer_id`
   - `to = <any valid PeerId, varied per request to bypass rate limiter>`
   - `listen_addrs = [attacker-controlled IP:port]` (1–24 addresses)
   - `sync_route = []`
4. R evaluates `route.last() == None`, checks `local_peer_id != V.peer_id` → calls `forward_delivered(V.peer_id)`.
5. R finds V in `peer_registry` and sends the crafted message to V.
6. V evaluates `route.last() == None`, checks `local_peer_id == V.peer_id` → enters "target peer" branch.
7. V calls `inflight_requests.remove(&content.to)`. If an entry exists, V calls `try_nat_traversal(ttl, [attacker-controlled IP:port])`, making ~150 TCP connection attempts over 30 seconds to the attacker's chosen endpoint.
8. Repeat step 3 with a new `content.to` value to bypass the `forward_rate_limiter` and sustain injection at up to 30 msg/sec.

**Invariant/fuzz test plan:**

- Property: for any `ConnectionRequestDelivered` message received by a relay, `forward_delivered` must only be called if the relay's `forwarded_requests` set contains `(content.from, content.to)`.
- Fuzz: generate random `ConnectionRequestDelivered` messages with `route = []` and `from` set to a peer ID present in `peer_registry` but absent from any legitimate route state; assert that `forward_delivered` is never invoked.