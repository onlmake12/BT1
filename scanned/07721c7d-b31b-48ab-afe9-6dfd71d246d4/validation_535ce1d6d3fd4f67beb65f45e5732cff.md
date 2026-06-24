Audit Report

## Title
Unbounded `forward_rate_limiter` HashMap Growth via Spoofed `from=local_peer_id` in `ConnectionRequestDelivered` — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary
The `HolePunching` protocol's `forward_rate_limiter` (`HashMapStateStore<(PeerId, PeerId, u32)>`) accumulates one new entry per unique `(from, to, item_id)` key. An attacker who spoofs `from = local_peer_id` and varies `to` per message can insert entries at 30/second per session with no eviction until disconnect, because `retain_recent()` is never called during a live connection. The terminal code path returns `StatusCode::Ignore` with no ban, so the attacker is never disconnected for this behavior.

## Finding Description

**Root cause — `forward_rate_limiter` grows without bound during a live connection.**

`governor`'s `HashMapStateStore` inserts a new state cell for every previously-unseen key on `check_key`. The key space is `(PeerId, PeerId, u32)`. Because `msg_item_id` is a fixed constant per message type, the effective key space is `(from_peer_id, to_peer_id)`.

At `connection_request_delivered.rs:134–137`, `check_key` is called unconditionally before any routing logic:

```rust
if self
    .protocol
    .forward_rate_limiter
    .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
    .is_err()
```

An attacker who sends a unique `to` peer ID per message creates a new `HashMap` entry on every call.

**Why the spoofed path reaches `StatusCode::Ignore` with no ban.**

With `route = []`, `content.route.last()` is `None`, entering the `None` branch at line 147. The check at line 151 (`if self_peer_id != &content.from`) is `false` when `from = local_peer_id`, so the forward path is skipped. `inflight_requests.remove(&content.to)` returns `None` for any `to` not in the inflight map, and line 175 returns `StatusCode::Ignore`. The outer `received` handler at `mod.rs:145` only bans on `status.should_ban()`, which `StatusCode::Ignore` does not satisfy.

**Why entries are never evicted during a live connection.**

`retain_recent()` is called only in `disconnected` (`mod.rs:66–70`). The `notify` handler (`mod.rs:169–244`), which fires every `CHECK_INTERVAL` (5 minutes), cleans up `pending_delivered` and `inflight_requests` but never calls `retain_recent()` on either rate limiter. Entries inserted by the attacker accumulate for the entire duration of the connection.

**Outer rate limiter does not prevent map growth.**

The outer `rate_limiter` at `mod.rs:95–107` is keyed by `(session_id, msg.item_id())`. It limits each session to 30 `ConnectionRequestDelivered` messages per second. This throttles the insertion rate but does not prevent it — 30 new entries/second per session is the guaranteed growth rate.

## Impact Explanation

Memory exhaustion leading to node crash. At 30 entries/second per session, with each entry consuming approximately 200–300 bytes (two `PeerId` values at ~39 bytes each, a `u32`, `HashMap` overhead, and `governor` internal state), the map grows at roughly 6–9 KB/second per session. With multiple concurrent attacker sessions, growth scales linearly. Over a sustained connection (hours), this exhausts available memory and crashes the node. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

- Any peer that can establish a standard P2P connection can trigger this — no privilege, no key, no proof-of-work required.
- The local peer ID is publicly advertised via the identify protocol, making `from = local_peer_id` trivially obtainable.
- The attack requires only sustained message sending with unique `to` peer IDs (random bytes are sufficient).
- The outer rate limiter does not prevent the attack; it only sets the insertion rate at 30/second per session.
- Multiple sessions multiply the growth rate linearly.
- The attacker is never banned, so the connection can be maintained indefinitely.

## Recommendation

1. **Call `retain_recent()` periodically**: In the `notify` handler (`mod.rs:169`), add calls to `self.rate_limiter.retain_recent()` and `self.forward_rate_limiter.retain_recent()` on every `CHECK_INTERVAL` tick, not only on disconnect.
2. **Validate `from` against the sending session's authenticated peer ID**: Before calling `check_key`, reject messages where `content.from` does not match the actual peer ID of the sending session. This prevents spoofing of `from = local_peer_id` entirely.
3. **Cap the `forward_rate_limiter` map size**: Reject new entries when the map exceeds a configurable bound (e.g., 10,000 entries), or switch to a bounded LRU-backed state store.

## Proof of Concept

```
1. Attacker establishes a P2P connection to the victim node.
2. Attacker learns victim's local_peer_id via the identify protocol.
3. Attacker sends up to 30 ConnectionRequestDelivered messages per second:
     from        = local_peer_id   (spoofed as victim's own peer ID)
     route       = []              (empty)
     to          = random_peer_id_i (unique per message, e.g., random 39-byte PeerId)
     listen_addrs = [any valid addr with 1..=24 entries]
4. Per message execution path:
   a. Outer rate_limiter passes (first 30/sec for this session × item_id).
   b. forward_rate_limiter.check_key((local_peer_id, random_peer_id_i, ITEM_ID))
      → inserts new HashMap entry (key never seen before).
   c. route.last() == None → enters terminal branch.
   d. self_peer_id == content.from → skips forward path.
   e. inflight_requests.remove(random_peer_id_i) → None.
   f. Returns StatusCode::Ignore → no ban.
5. After N messages, forward_rate_limiter HashMap contains N entries.
6. notify() fires every 5 minutes but does NOT call retain_recent() → no eviction.
7. Entries accumulate until disconnect; with multiple sessions, growth is linear.
8. Assertion: map size grows monotonically at 30 entries/sec/session with no eviction
   and no ban until memory is exhausted.
```