Audit Report

## Title
Unauthenticated `ConnectionRequestDelivered` Relay Allows Targeted Peer Flooding via Bypassed `forward_rate_limiter` — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary
The `ConnectionRequestDeliveredProcess::execute()` method forwards `ConnectionRequestDelivered` messages to `route.last()` without verifying that the relay ever processed a corresponding `ConnectionRequest` for the same `(from, to)` pair. The `forward_rate_limiter` keyed on `(from, to, msg_item_id)` is trivially bypassed because both fields are attacker-controlled message body values. The only real constraint is the outer `rate_limiter` at 30 req/s per session, allowing an attacker with one relay connection to flood any peer connected to that relay at up to 30 HolePunching messages/second.

## Finding Description

In `connection_request_delivered.rs`, `execute()` checks the `forward_rate_limiter` at L134–145 keyed by `(content.from, content.to, self.msg_item_id)`, then unconditionally routes at L147–148:

```rust
match content.route.last() {
    Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
```

`content.from`, `content.to`, and `content.route` are all parsed directly from the attacker-supplied message body with no cross-reference to any relay state. The `forward_rate_limiter` (1 req/s per `(from, to)` pair) is bypassed by simply varying `from`/`to` across requests — each unique pair gets its own bucket.

`forward_delivered()` at L182–212 resolves the attacker-supplied `peer_id` from `route.last()` against the live peer registry and calls `send_message_to()` unconditionally if the peer is connected.

The `inflight_requests` and `pending_delivered` state in `mod.rs` L42–44 are only consulted in the `None` branch of `route.last()` (L160), i.e., only when the local node is the terminal `from` peer — never when acting as a relay.

The outer `rate_limiter` in `mod.rs` L95–107, keyed by `(session_id, msg.item_id())` at 30 req/s, is the sole real constraint and cannot be bypassed. It caps the attacker at exactly 30 forwarded messages/second per relay connection.

## Impact Explanation

This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker with a single TCP connection to relay R can direct R to forward 30 HolePunching messages/second to any peer V connected to R. With N relay connections the rate scales to 30N messages/second against one or many victims. The victim's own per-session rate limiter (also 30/s per relay session) does not provide a global cap, so each additional relay connection the attacker holds adds another 30/s stream to the victim. The victim must execute `DeliverdContent::try_from`, peer registry reads, and rate-limiter key checks for every message, consuming CPU and inbound bandwidth at attacker-controlled scale with no corresponding cost beyond maintaining TCP connections.

## Likelihood Explanation

Exploitation requires only a standard P2P connection to any relay running the HolePunching protocol — no keys, hashpower, or special privileges. The victim's `PeerId` is public information on the CKB P2P network. The `forward_rate_limiter` bypass requires only that the attacker increment a counter in the `from` or `to` field per message, which is trivial. The exploit is repeatable and persistent as long as the attacker maintains relay connections.

## Recommendation

Before calling `forward_delivered()` in the relay path, verify that the relay previously forwarded a `ConnectionRequest` for the same `(from, to)` pair:

- In `ConnectionRequestProcess::forward_message`, insert `(from, to)` with a timestamp into a `forwarded_requests: HashMap<(PeerId, PeerId), u64>` on the `HolePunching` struct.
- In `ConnectionRequestDeliveredProcess::execute()`, when `route.last()` is `Some`, check that `forwarded_requests` contains a recent (non-expired) entry for `(content.from, content.to)` before calling `forward_delivered`.
- Remove the entry after forwarding the delivered message, or expire it after the existing `TIMEOUT` (5 minutes), to prevent replay.
- Add cleanup of `forwarded_requests` in the `notify` timer alongside the existing `inflight_requests` and `pending_delivered` cleanup at `mod.rs` L173–175.

## Proof of Concept

```
1. Attacker A establishes a standard P2P connection to relay R.
2. A observes (via normal peer exchange) that victim V is connected to R.
3. A sends ~30 ConnectionRequestDelivered messages/second to R, each with:
     - route       = [V.peer_id]
     - from        = unique_random_peer_id_i   (new value each message)
     - to          = unique_random_peer_id_i'  (new value each message)
     - listen_addrs = [valid multiaddr embedding to's peer_id]
     - sync_route  = []
4. For each message, R's execute() passes:
     - DeliverdContent::try_from  → succeeds (valid encoding)
     - listen_addrs length check  → passes (1 addr)
     - route length check         → passes (1 hop ≤ MAX_HOPS)
     - forward_rate_limiter check → passes (new (from,to) bucket each time)
     - route.last() = V.peer_id  → calls forward_delivered(V.peer_id)
     - peer_registry lookup       → returns V's session_id
     - send_message_to(V, ...)   → message delivered to V
5. V receives 30 HolePunching messages/second from R.
6. V processes each: parse + rate-limiter check + inflight_requests lookup → StatusCode::Ignore.
7. No ConnectionRequest for any of these (from, to) pairs was ever forwarded by R.
Verification: instrument forward_delivered() call count at R and received-message count at V; assert they match at ~30/s with zero prior ConnectionRequest state.
```