Audit Report

## Title
Unauthenticated Route-Bypass Message Injection via Missing Route Membership Check in `ConnectionRequestDeliveredProcess::execute` — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary

In `ConnectionRequestDeliveredProcess::execute()`, when `content.route` is empty, the relay unconditionally forwards the message to the attacker-supplied `content.from` peer ID without verifying it ever participated in a legitimate hole-punching route for that `(from, to)` pair. Any unprivileged peer connected to a relay can exploit this to inject crafted `ConnectionRequestDelivered` messages to any other peer connected to the same relay at up to 30 messages per second, with the `forward_rate_limiter` trivially bypassed by varying `content.to`. This constitutes a low-cost, scalable bad design capable of causing CKB P2P network congestion.

## Finding Description

**Root cause — missing route membership check (L147–153, `connection_request_delivered.rs`):**

```rust
match content.route.last() {
    Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
    None => {
        let self_peer_id = self.protocol.network_state.local_peer_id();
        if self_peer_id != &content.from {
            self.forward_delivered(&content.from).await  // ← no route membership check
```

When `route` is empty, the only guard is `local_peer_id != content.from`. Since the attacker sets `content.from` to the victim's peer ID (not the relay's), this check always passes and `forward_delivered(&content.from)` is called unconditionally. No state is consulted to confirm the relay ever forwarded a `ConnectionRequest` for this `(from, to)` pair. There is no `forwarded_requests` set or equivalent in `HolePunching` struct.

**Unconditional forwarding (L182–213, `connection_request_delivered.rs`):**

`forward_delivered` performs a `peer_registry` read-lock lookup for the attacker-supplied peer ID and, if connected, sends the full attacker-crafted message. The helper `forward_delivered(self.message)` at L191 passes the original message through with `route` stripped to empty (already empty), so the victim receives `route = []`, `from = victim_peer_id`, `listen_addrs = attacker-controlled`.

**Rate limiter bypass (L134–145, `connection_request_delivered.rs`):**

The `forward_rate_limiter` is keyed on `(content.from, content.to, self.msg_item_id)`. All three values are attacker-controlled; `msg_item_id` is fixed per message type. By varying `content.to` across requests, the attacker generates distinct keys and bypasses the 1 req/sec per-key limit. The only remaining cap is the session-level `rate_limiter` at L95–107 of `mod.rs`, keyed on `(session_id, item_id)`, allowing 30 req/sec.

**Victim-side resource consumption (L160–176, `connection_request_delivered.rs`; L49–115, `component/mod.rs`):**

When the victim receives the injected message (`route = []`, `from = victim_peer_id`), it enters the `else` branch (L154–177) and calls `inflight_requests.remove(&content.to)`. If a matching entry exists (populated by the `notify` timer at L239–242 of `mod.rs` when the node is under-connected), it spawns `try_nat_traversal(ttl, content.listen_addrs)` — a 30-second loop making TCP connection attempts every ~200ms (~150 attempts) to fully attacker-controlled IP:port endpoints.

**No existing mitigation:** The `HolePunching` struct (L38–47, `mod.rs`) contains no `forwarded_requests` set or any state tracking which `(from, to)` pairs the node has legitimately relayed.

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

The relay-side injection is unconditional: any unprivileged peer connected to a relay can cause that relay to forward up to 30 crafted `ConnectionRequestDelivered` messages per second to any other peer connected to the same relay, with zero legitimate routing state required. By connecting to N relay nodes simultaneously, an attacker multiplies this rate to 30×N messages per second. Each injected message consumes processing resources on the victim. When `inflight_requests` entries exist on the victim, each injected message additionally spawns a long-lived async task opening TCP sockets to attacker-chosen endpoints, amplifying resource exhaustion. This is a low-cost, scalable bad design that can cause CKB P2P network congestion.

## Likelihood Explanation

**Relay-side injection:** Requires only that the attacker be a connected P2P peer (any unprivileged node) and know the peer ID of a victim also connected to the same relay. Peer IDs are publicly observable on the CKB P2P network. No special privileges, leaked keys, or victim mistakes are required.

**Victim-side resource escalation:** Requires additionally that the victim has an active `inflight_requests` entry for `content.to`. This is populated automatically by the `notify` timer (every `CHECK_INTERVAL` = 5 minutes, L25 `mod.rs`) when the victim has fewer outbound connections than `max_outbound` — a routine background condition for any under-connected node. The attacker can vary `content.to` across requests (which simultaneously bypasses the rate limiter) to probe for valid entries.

The attack is repeatable: after `inflight_requests` entries are consumed via `remove`, the victim repopulates them at the next `notify` tick.

## Recommendation

1. In `HolePunching`, maintain a bounded `HashSet<(PeerId, PeerId)>` of `(from, to)` pairs for which the node has previously forwarded a `ConnectionRequest` (populated in `ConnectionRequestProcess::execute` → `forward_message`).
2. In the `None` branch of `ConnectionRequestDeliveredProcess::execute`, before calling `forward_delivered(&content.from)`, verify `(content.from, content.to)` is present in this set; otherwise return `StatusCode::Ignore`.
3. Expire entries from the set after `TIMEOUT` (already defined as 5 minutes in `mod.rs`) to bound memory usage.
4. Re-key the `forward_rate_limiter` on `(sender_session_id, content.from)` rather than `(content.from, content.to, item_id)` to prevent bypass via varying `content.to`.

## Proof of Concept

**Minimal manual steps:**

1. Attacker peer A connects to relay node R via the CKB P2P protocol.
2. Victim peer V is also connected to R (V's peer ID is observable from the P2P network).
3. A sends a `ConnectionRequestDelivered` message to R with:
   - `route = []` (empty)
   - `from = V.peer_id`
   - `to = <any valid PeerId, varied per request to bypass rate limiter>`
   - `listen_addrs = [attacker-controlled IP:port with V.peer_id appended]` (1–24 addresses)
   - `sync_route = []`
4. R evaluates `route.last() == None`, checks `local_peer_id != V.peer_id` → calls `forward_delivered(V.peer_id)`.
5. R finds V in `peer_registry` and sends the crafted message to V.
6. V evaluates `route.last() == None`, checks `local_peer_id == V.peer_id` → enters "target peer" branch.
7. V calls `inflight_requests.remove(&content.to)`. If an entry exists, V calls `try_nat_traversal(ttl, [attacker-controlled IP:port])`, making ~150 TCP connection attempts over 30 seconds.
8. Repeat step 3 with a new `content.to` value to bypass `forward_rate_limiter` and sustain injection at up to 30 msg/sec.

**Invariant/fuzz test plan:**

- Property: for any `ConnectionRequestDelivered` message received by a relay with `route = []`, `forward_delivered` must only be called if the relay's `forwarded_requests` set contains `(content.from, content.to)`.
- Fuzz: generate random `ConnectionRequestDelivered` messages with `route = []` and `from` set to a peer ID present in `peer_registry` but absent from any legitimate route state; assert `forward_delivered` is never invoked.