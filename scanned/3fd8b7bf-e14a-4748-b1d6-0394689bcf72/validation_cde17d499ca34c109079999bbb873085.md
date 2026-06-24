Audit Report

## Title
Unbounded `pending_delivered` HashMap Growth via Spoofed `from` PeerIds in `ConnectionRequest` — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
A connected peer can insert an unbounded number of entries into the `pending_delivered` HashMap by sending `ConnectionRequest` messages with unique, attacker-controlled `from` PeerIds targeting the victim node. Both the `forward_rate_limiter` and the dedup guard in `respond_delivered()` are bypassed by unique `from` values, leaving only the per-session 30 req/sec `rate_limiter` as a bound. Over the 5-minute `notify()` prune interval, a single session can accumulate up to 9,000 entries (~6.9 MB), scaling linearly with concurrent sessions.

## Finding Description
**Root cause:** `pending_delivered` is initialized as an unbounded `HashMap::new()` with no capacity cap. Pruning occurs only in `notify()` every `CHECK_INTERVAL` (5 minutes).

**Rate limiter bypass chain:**

1. `rate_limiter` (mod.rs L95–107) is keyed by `(session_id, msg.item_id())` — 30 req/sec per session. This is the only effective bound.

2. `forward_rate_limiter` (connection_request.rs L132–143) is keyed by `(content.from, content.to, msg_item_id)`. Each unique `from` PeerId creates a fresh key, so the 1 req/sec limit is never triggered.

3. The dedup guard in `respond_delivered()` (connection_request.rs L161–167) checks `pending_delivered.get(&from_peer_id)`. With a unique `from` per message, this always misses.

4. After a successful `send_message_to` back to the attacker's session (L226–232), the unconditional insert fires (L234–237):
   ```rust
   self.protocol.pending_delivered.insert(from_peer_id, (remote_listens, now));
   ```

**Preconditions satisfied trivially:**
- `content.to` = victim's peer ID (publicly discoverable via identify protocol)
- `content.from` = fresh random PeerId per message
- `content.listen_addrs` contains at least one TCP/IPv4 or IPv6 address (e.g., `127.0.0.1:1234`)
- Attacker keeps the session open so `send_message_to` succeeds

**Accumulation:** 30 msg/sec × 300 sec = 9,000 entries per session before `notify()` prunes via `retain()` (mod.rs L173–174).

The `forward_rate_limiter`'s internal `HashMapStateStore` also accumulates one entry per unique `(from, to, item_id)` key during the session, compounding memory growth. `retain_recent()` is only called on `disconnected()` (mod.rs L67–69).

## Impact Explanation
**Medium — Suboptimal implementation of CKB state storage mechanism.**

Per-session memory growth:
- `pending_delivered`: 9,000 entries × (~38 B PeerId + 24 × ~30 B Multiaddr + 8 B timestamp) ≈ **6.9 MB**
- `forward_rate_limiter` internal state: 9,000 entries × ~138 B ≈ **1.2 MB**
- Total per session: ~**8.1 MB**

With `max_inbound_peers = max_peers − max_outbound_peers` (typically ~117 inbound slots), a coordinated attack using all inbound slots yields ~117 × 8.1 MB ≈ **~950 MB** of unbounded map growth before the next prune cycle. This violates the invariant that protocol state maps must be bounded in size and constitutes a suboptimal implementation of the node's P2P state storage mechanism. While this does not trivially crash a well-provisioned node in a single session, it degrades memory health and can cause OOM pressure on resource-constrained deployments.

## Likelihood Explanation
- Requires only a standard inbound P2P connection — no privilege escalation needed.
- Victim's peer ID is publicly observable via the identify protocol.
- Attacker generates random PeerIds in software; no cryptographic work required.
- Attack is repeatable across sessions and scales with the number of concurrent attacker-controlled connections.
- The only mitigation in place (the 30 req/sec rate limiter) is intentionally permissive and does not bound map size.

## Recommendation
Add a capacity cap in `respond_delivered()` before the insert:
```rust
const MAX_PENDING_DELIVERED: usize = 1024;

if self.protocol.pending_delivered.len() >= MAX_PENDING_DELIVERED {
    return StatusCode::TooManyRequests.with_context("pending_delivered capacity exceeded");
}
```
Alternatively, key the `forward_rate_limiter` on `(session_id, to_peer_id)` rather than `(from, to, item_id)` to prevent a single session from creating unbounded unique rate-limiter keys. Both fixes should be applied together.

## Proof of Concept
```rust
// Minimal state test sketch
let mut hp = HolePunching::new(network_state_configured_as_victim());
for _ in 0..10_000 {
    let from = PeerId::random();
    let msg = build_connection_request(
        from,
        victim_peer_id,
        vec!["/ip4/127.0.0.1/tcp/1234".parse().unwrap()],
    );
    hp.received(/* context with open session */, msg).await;
}
// Before notify() fires (within 5-minute window):
assert!(hp.pending_delivered.len() >= 9_000);
// forward_rate_limiter internal map also has ~9,000 entries
```
A fuzz test varying `content.from` while holding `content.to = local_peer_id` and a valid TCP listen address will reliably reproduce unbounded growth up to the rate-limiter ceiling of 9,000 entries per session per 5-minute window.