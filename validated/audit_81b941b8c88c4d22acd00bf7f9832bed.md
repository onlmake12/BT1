Audit Report

## Title
Unbounded `pending_delivered` HashMap Growth via Unique-`from` `ConnectionRequest` Flood — (`network/src/protocols/hole_punching/mod.rs`)

## Summary
The `HolePunching` protocol's `pending_delivered: HashMap<PeerId, PendingDeliveredInfo>` has no capacity bound. An attacker connected to a victim node can flood it with `ConnectionRequest` messages using a unique `from` PeerId per message, bypassing all three existing guards and inserting one new map entry per message. Eviction only occurs every 5 minutes, enabling unbounded heap growth that can crash the node via OOM.

## Finding Description
**Root cause:** `pending_delivered` is declared as an unbounded `HashMap<PeerId, PendingDeliveredInfo>` at `mod.rs:44`. Insertion at `connection_request.rs:234–237` has no size cap.

**Guard 1 — session rate limiter** (`mod.rs:95–107`): keyed by `(session_id, msg.item_id())`, permits 30 `ConnectionRequest` messages/sec per session. An attacker sending at exactly 30/sec passes this check every time.

**Guard 2 — `forward_rate_limiter`** (`connection_request.rs:132–143`): keyed by `(content.from, content.to, msg_item_id)`. Because the attacker uses a fresh random `from` PeerId for each message, every message gets its own new rate-limit bucket — the limiter is fully bypassed.

**Guard 3 — deduplication** (`connection_request.rs:161–167`): skips processing only if the **same** `from_peer_id` was seen within `HOLE_PUNCHING_INTERVAL`. With a unique `from` per message, this check never fires.

**Insertion path** (`connection_request.rs:234–237`): after `send_message_to` succeeds, `pending_delivered.insert(from_peer_id, (remote_listens, now))` is called unconditionally with no size guard.

**Eviction** (`mod.rs:173–175`): `retain(...)` fires only every `CHECK_INTERVAL = 300s` inside `notify()`.

Preconditions that are all trivially met:
- `content.to == self_peer_id` — victim's PeerId is publicly advertised via P2P discovery.
- `listen_addrs` non-empty and ≤ 24 — attacker supplies one valid `/ip4/x.x.x.x/tcp/port` address.
- `max_hops ≤ 6` — attacker sets any value ≤ 6.
- `route` does not contain victim's PeerId — attacker sends empty route.
- `send_message_to` succeeds — attacker is connected.

## Impact Explanation
**High — Vulnerabilities which could easily crash a CKB node.**

Per session per 5-minute window: 30 msg/sec × 300 s = 9,000 entries. Each entry stores up to `ADDRS_COUNT_LIMIT = 24` `Multiaddr` objects (~100 bytes each with P2P suffix), yielding ~2.4 KB per entry, or ~21.6 MB per session per window. Multiple concurrent attacker sessions multiply the rate linearly. The victim also sends a `ConnectionRequestDelivered` reply for every accepted message, amplifying outbound bandwidth. Sustained attack leads to OOM and node crash.

## Likelihood Explanation
Any peer with a standard P2P connection to the victim can execute this attack. The victim's PeerId is publicly available via the P2P discovery layer. No special privileges, keys, or hashpower are required. The attacker constructs valid `ConnectionRequest` messages with random `from` PeerIds and one valid TCP listen address — both trivially achievable. The attack is fully automatable and repeatable.

## Recommendation
1. **Cap `pending_delivered`**: Enforce a maximum size (e.g., 1,000 entries). On overflow, reject new insertions or evict the oldest entry.
2. **Rate-limit insertions per session**: Track how many `pending_delivered` entries originated from each session and cap that count.
3. **Shorten or decouple the eviction interval**: Run `pending_delivered` eviction more frequently than `CHECK_INTERVAL`, or evict eagerly on insertion when the map exceeds a threshold.
4. **Validate `from` PeerId against the sending session**: Reject `ConnectionRequest` messages where `from` does not match the session's authenticated peer identity, eliminating spoofed `from` PeerIds entirely.

## Proof of Concept
```
1. Connect to victim node V (obtain its PeerId P from the P2P discovery layer).
2. From a single session, send 30 ConnectionRequest messages per second:
     from = PeerId::random()                    // unique each time
     to   = P                                   // victim's local_peer_id
     listen_addrs = [/ip4/1.2.3.4/tcp/1234]    // one valid TCP addr
     max_hops = 6
     route = []
3. After 300 seconds (before the next notify() fires):
     assert pending_delivered.len() == 9000
     assert no eviction has occurred
4. Observe victim heap growth (~21 MB per session) and degraded HashMap performance.
5. Open additional sessions to multiply the effect until OOM.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L44-44)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
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

**File:** network/src/protocols/hole_punching/mod.rs (L173-175)
```rust
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L132-143)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionRequest",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionRequest");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-167)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```
