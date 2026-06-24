Audit Report

## Title
Missing `from != to` Peer Identity Validation Enables Amplified Gossip Flooding in Hole Punching Protocol - (File: `network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary

`ConnectionRequestProcess::execute()` contains no guard that `content.from != content.to`. An attacker with a single established session can craft `ConnectionRequest` messages with identical `from` and `to` peer IDs, causing every relay node to invoke `forward_message()` and broadcast to `sqrt(peers)` nodes per hop across up to `MAX_HOPS = 6` hops. With 30 distinct spoofed `(X, X)` pairs per second (the session-level cap), a single attacker connection generates up to `30 × N` forwarded messages per second across the entire network, where N is the node count.

## Finding Description

**Root cause — confirmed absent check:**

`connection_request.rs` lines 110–153 validate listen address count, `max_hops` ceiling, route length, and rate limits, but contain no `content.from != content.to` guard:

```rust
// Lines 110–153: no from != to check anywhere
pub(crate) async fn execute(mut self) -> Status {
    let content = match RequestContent::try_from(&self.message) { ... };
    if content.listen_addrs.len() > ADDRS_COUNT_LIMIT || content.listen_addrs.is_empty() { ... }
    if content.max_hops > MAX_HOPS { ... }
    if content.route.len() > MAX_HOPS as usize { ... }
    if content.route.contains(self_peer_id) { ... }
    // rate limiter keyed by (from, to, msg_item_id) — does NOT reject from == to
    if self.protocol.forward_rate_limiter.check_key(&(content.from.clone(), content.to.clone(), ...)).is_err() { ... }

    if self_peer_id == &content.to {
        self.respond_delivered(content.from, &content.to, content.listen_addrs).await
    } else if content.max_hops == 0u8 {
        StatusCode::ReachedMaxHops.into()
    } else {
        self.forward_message(self_peer_id, &content.to).await   // broadcasts to sqrt(peers) nodes
    }
}
```

**Case 1 — relay node (`self_peer_id != content.to`):**

`forward_message()` is called. Because the spoofed `to` peer ID does not exist in the peer registry, the `None` branch at line 273 executes, broadcasting to `sqrt(total_peers)` connected nodes via `filter_broadcast`. Each receiving relay node repeats this logic, decrementing `max_hops` and appending itself to `route`. The route deduplication (`content.route.contains(self_peer_id)`) prevents a single node from processing the same message twice, but does not prevent the message from reaching every other node in the network.

The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)`. Since `msg_item_id` is a fixed per-type constant (not per-message random), the key for all `ConnectionRequest` messages with `from == to == X` is `(X, X, fixed_id)`, allowing exactly 1 forward per second per relay node per distinct X. With 30 distinct X values per second (session cap), each relay node forwards 30 messages per second — and this load is replicated across all N nodes in the network.

**Case 2 — target node (`self_peer_id == content.to == content.from`):**

`respond_delivered()` is called. At lines 234–237, a self-referential entry is inserted:

```rust
self.protocol.pending_delivered.insert(from_peer_id, (remote_listens, now));
```

Here `from_peer_id == to_peer_id == self_peer_id`. This entry persists for `TIMEOUT = 5 minutes`. A subsequent `ConnectionSync` with `from == to == X` arriving at this node causes `pending_delivered.get(&content.from)` (lines 111–115 of `connection_sync.rs`) to return the attacker-controlled `remote_listens`, spawning async NAT traversal tasks to attacker-supplied addresses.

**Existing guards and why they fail:**

- Session-level rate limiter (30 req/sec per `(session_id, item_id)`): limits the attacker to 30 initial messages per second, but each message fans out to N nodes.
- `forward_rate_limiter` keyed by `(from, to, item_id)` at 1 req/sec: limits per-relay forwarding to 1/sec per distinct `(X, X)` pair, but 30 distinct pairs saturate this at 30 forwards/sec per relay node.
- Route deduplication: prevents loops but does not prevent full network propagation.
- `pending_delivered` TTL cleanup: runs every 5 minutes (`CHECK_INTERVAL`), not per-message.

## Impact Explanation

A single attacker connection generating 30 messages per second causes `30 × N` forwarded messages per second across the entire CKB P2P overlay. For a network of 1,000 nodes this is 30,000 protocol messages per second; for 10,000 nodes, 300,000 per second — all originating from one session. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

Any peer that completes a session handshake with a CKB node supporting the `HolePunching` protocol can send this message. The `from` and `to` fields are free-form bytes with no cryptographic binding to the actual session. No key material, elevated role, or victim mistake is required. The attack is repeatable continuously at 30 distinct spoofed pairs per second per attacker connection, and multiple attacker connections multiply the effect linearly.

## Recommendation

Add an explicit identity check immediately after parsing `content` in `ConnectionRequestProcess::execute()`, and mirror it in `ConnectionRequestDeliveredProcess::execute()` and `ConnectionSyncProcess::execute()`:

```rust
if content.from == content.to {
    return StatusCode::InvalidFromPeerId
        .with_context("from and to peer ids must be different");
}
```

Since `InvalidFromPeerId` maps to status code 411 (4xx range), `should_ban()` in `status.rs` will return `Some(BAD_MESSAGE_BAN_TIME)`, causing the offending session to be banned for 24 hours — providing a strong disincentive against repeated attempts.

## Proof of Concept

1. Establish a session with a CKB node that has the `HolePunching` protocol enabled.
2. Choose any 39-byte multihash byte sequence as peer ID `X`. Set both `from` and `to` to `X`.
3. Construct a `ConnectionRequest` molecule message: `from = X`, `to = X`, `max_hops = 6`, `route = []`, `listen_addrs` = 1–24 valid TCP/IP multiaddresses with peer ID `X` appended.
4. Send the message over the hole punching protocol stream.
5. **Relay path**: Observe via network monitoring that the message is forwarded to `sqrt(peers)` nodes, each of which forwards to `sqrt(peers)` more nodes, propagating until `max_hops` reaches 0 or all nodes have been visited.
6. Repeat with 30 distinct `X` values per second to sustain amplification at the session rate limit.
7. **Target path**: If `X` matches the local peer ID of any node, observe `pending_delivered[X]` is set with attacker-supplied addresses. Send a `ConnectionSync` with `from = to = X` routed to that node; observe NAT traversal tasks spawned to attacker-controlled addresses.

**Verification**: A unit test can be written in `network/src/protocols/hole_punching/component/mod.rs` alongside the existing `test_route` test, constructing a `ConnectionRequest` with `from == to` and asserting that `execute()` returns `StatusCode::InvalidFromPeerId` once the fix is applied — and confirming it currently does not.