Audit Report

## Title
Unbounded `pending_delivered` HashMap Growth via Unique-`from` ConnectionRequest Flood — (`network/src/protocols/hole_punching/component/connection_request.rs`, `network/src/protocols/hole_punching/mod.rs`)

## Summary
The `forward_rate_limiter` in `ConnectionRequestProcess::execute()` is keyed on the attacker-controlled wire field `content.from`, allowing complete bypass by rotating a unique `PeerId` per message. Combined with the `respond_delivered` dedup check being identically bypassed, an unprivileged peer can insert entries into `pending_delivered` at the full outer rate-limit speed (30/sec) with no cap, causing unbounded heap growth until the 5-minute cleanup tick fires. Sustained or multi-session attacks can OOM-crash a CKB node.

## Finding Description

**Outer rate limiter (effective):** `received()` checks `rate_limiter` keyed on `(session_id, msg.item_id())` — a `(PeerIndex, u32)` pair tied to the actual TCP session. This correctly caps one session at 30 `ConnectionRequest` messages per second. [1](#0-0) 

**Inner `forward_rate_limiter` (bypassed):** `execute()` checks `forward_rate_limiter` keyed on `(content.from.clone(), content.to.clone(), self.msg_item_id)`. `content.from` is raw wire data parsed from the message with no binding to the actual session identity. An attacker who sends a fresh random `PeerId` as `from` in every message produces a distinct key each time, so `check_key` never returns `Err` and the limiter is unconditionally passed. [2](#0-1) 

**`respond_delivered` dedup check (bypassed):** The only guard inside `respond_delivered` looks up `from_peer_id` in `pending_delivered`. With a unique `from` per message, no existing entry is ever found, so the early-return is never taken. [3](#0-2) 

**Precondition for `respond_delivered` to be reached:** The branch `if self_peer_id == &content.to` must be true. The attacker sets `to` to the victim's own `PeerId`, which is public information exchanged during P2P handshake. [4](#0-3) 

**Unbounded insertion:** After all guards pass, `respond_delivered` unconditionally inserts `(remote_listens, now)` into `pending_delivered` keyed by `from_peer_id`. There is no size cap on the map. [5](#0-4) 

Each entry is `PendingDeliveredInfo = (Vec<Multiaddr>, u64)`, and the attacker can include up to `ADDRS_COUNT_LIMIT = 24` addresses per message. [6](#0-5) 

**Cleanup only every 5 minutes:** `notify()` prunes `pending_delivered` by retaining entries younger than `TIMEOUT = 5 * 60 * 1000 ms`. Between ticks, every inserted entry accumulates. [7](#0-6) 

**Secondary growth — `forward_rate_limiter` internal state:** The limiter uses `governor::state::keyed::HashMapStateStore<(PeerId, PeerId, u32)>`. Each unique `(from, to, msg_item_id)` tuple allocates a new entry in this store. `retain_recent()` is called only in `disconnected()`, never in `notify()`, so the store grows continuously for the entire lifetime of a live attacker session. [8](#0-7) 

## Impact Explanation

In a single 5-minute window, one attacker session can insert up to `30 × 300 = 9,000` entries into `pending_delivered`. With 24 `Multiaddr` objects per entry (~800–1,250 bytes per entry), this yields approximately 7–11 MB per session per window. The `forward_rate_limiter` internal store accumulates the same 9,000 keys and is never pruned during the session. Multiple concurrent attacker sessions cause linear scaling. Sustained over hours, this leads to OOM crash and node unavailability. This matches the allowed CKB bounty impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

The attack requires only a single valid P2P connection — no proof-of-work, no trusted role, no key material beyond what is exchanged during normal handshake. The victim's `PeerId` is public. Generating unique valid `PeerId` bytes is trivial. The attacker must include at least one valid TCP multiaddr (e.g., `/ip4/1.2.3.4/tcp/1234`) to pass the `remote_listens.is_empty()` guard, which is also trivial. The attack is fully repeatable and requires no victim interaction. [9](#0-8) 

## Recommendation

1. **Cap `pending_delivered` size**: Enforce a hard upper bound (e.g., 1,024 entries). Reject new insertions or evict LRU entries when the cap is reached.
2. **Key `forward_rate_limiter` on session identity, not wire `from`**: Replace `(content.from.clone(), content.to.clone(), self.msg_item_id)` with `(self.peer, self.msg_item_id)` or `(self.peer, content.to.clone(), self.msg_item_id)` so the rate limit is tied to the actual TCP session and cannot be bypassed by rotating the attacker-controlled `from` field.
3. **Prune `forward_rate_limiter` periodically**: Call `self.forward_rate_limiter.retain_recent()` inside `notify()` in addition to `disconnected()`, to prevent unbounded growth of the limiter's internal state during long-lived sessions. [7](#0-6) 

## Proof of Concept

```rust
// Pseudocode unit test
let mut protocol = HolePunching::new(network_state);
let session_id = PeerIndex::new(1);
let victim_peer_id = protocol.network_state.local_peer_id().clone();

for i in 0..9000 {
    let unique_from = PeerId::random(); // unique per iteration — new forward_rate_limiter key
    let msg = build_connection_request(
        from: unique_from,
        to: victim_peer_id.clone(),   // triggers respond_delivered branch
        listen_addrs: vec!["/ip4/1.2.3.4/tcp/1234".parse().unwrap()], // passes is_empty() guard
        max_hops: 1,
        route: vec![],                // does not contain self_peer_id
    );
    // Outer rate_limiter allows 30/sec; simulate at that rate
    protocol.received(session_id, msg).await;
}

// notify() has not fired yet (< 5 minutes elapsed)
assert!(protocol.pending_delivered.len() <= SOME_BOUND);
// FAILS: len == 9000, no bound enforced
```

Each iteration uses a fresh `from` PeerId, bypassing `forward_rate_limiter` (new key each time) and the `respond_delivered` dedup check (no existing entry in `pending_delivered`). After 9,000 iterations without a `notify()` tick, `pending_delivered.len() == 9,000` with no bound enforced. [2](#0-1) [3](#0-2) [5](#0-4)

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L27-30)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes

type PendingDeliveredInfo = (Vec<Multiaddr>, u64);
```

**File:** network/src/protocols/hole_punching/mod.rs (L66-70)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
        debug!("HolePunching.disconnected session={}", context.session.id);
    }
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

**File:** network/src/protocols/hole_punching/mod.rs (L169-175)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let status = self.network_state.connection_status();

        let now = unix_time_as_millis();
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L196-219)
```rust
        let remote_listens: Vec<Multiaddr> = remote_listens
            .into_iter()
            .filter_map(|addr| match find_type(&addr) {
                TransportType::Memory
                | TransportType::Onion
                | TransportType::Ws
                | TransportType::Wss
                | TransportType::Tls => None,
                TransportType::Tcp => {
                    if addr
                        .iter()
                        .any(|p| matches!(p, Protocol::Ip4(_) | Protocol::Ip6(_)))
                    {
                        Some(addr)
                    } else {
                        None
                    }
                }
            })
            .collect();

        if remote_listens.is_empty() {
            return StatusCode::Ignore.with_context("remote listen address is empty");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```
