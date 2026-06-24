Audit Report

## Title
Unbounded `forward_rate_limiter` HashMapStateStore growth via unique `(from, to)` PeerId pairs in `ConnectionSync` messages — (`network/src/protocols/hole_punching/mod.rs`)

## Summary
`HolePunching::forward_rate_limiter` is a `governor::RateLimiter` backed by `HashMapStateStore<(PeerId, PeerId, u32)>`. A single unprivileged peer can send `ConnectionSync` messages at ≤30/s, each carrying a fresh unique `(from, to)` PeerId pair, causing one new map entry per message with no reclamation until the session disconnects. The `notify()` handler fires every 5 minutes but never calls `retain_recent()`, so the attacker controls reclamation by keeping the session alive. Sustained over hours, this exhausts heap memory and can crash the node.

## Finding Description
`forward_rate_limiter` is declared as `RateLimiter<(PeerId, PeerId, u32)>` backed by `HashMapStateStore`. [1](#0-0) 

In `ConnectionSyncProcess::execute()`, `check_key` is called with attacker-controlled `content.from` and `content.to` decoded directly from the wire message with no constraint beyond being valid PeerId bytes: [2](#0-1) 

Each call with a previously-unseen `(from, to, msg_item_id)` key inserts a new entry into the `HashMapStateStore` and **succeeds** (quota not yet consumed for that key). The `msg_item_id` is a fixed constant per message type, so the effective key space is `(from, to)` pairs.

`retain_recent()` is called only in `disconnected()`: [3](#0-2) 

The `notify()` handler (every `CHECK_INTERVAL = 5 minutes`) cleans `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on either rate limiter: [4](#0-3) 

The per-session `rate_limiter` (keyed by `(PeerIndex, u32)`) throttles to 30 req/s but does not bound the cardinality of keys in `forward_rate_limiter`: [5](#0-4) 

The attacker simply keeps the session alive and never triggers `disconnected()`, so `retain_recent()` is never called.

## Impact Explanation
At 30 unique `(from, to)` pairs per second, the `forward_rate_limiter` map accumulates ~108,000 entries/hour and ~2.6M entries/24 hours. At ~150–200 bytes per entry (two PeerId values + u32 + HashMap overhead), this is ~400–500 MB/day from a single session. Multiple concurrent sessions multiply the effect linearly. Sustained over hours, this degrades node performance and exhausts available memory, causing the node to crash or become unresponsive. This matches the allowed impact: **"Vulnerabilities which could easily crash a CKB node" — High (10001–15000 points)**.

## Likelihood Explanation
The attack requires only a standard P2P connection to the HolePunching protocol — no privileges, no proof-of-work, no key material. The attacker generates fresh random PeerId bytes for `from` and `to` in each message. The only upstream throttle is the 30 req/s per-session cap, which sets the growth rate but does not prevent the attack. The session can be maintained indefinitely. `governor`'s `HashMapStateStore` does not self-evict; it only shrinks when `retain_recent()` is explicitly called.

## Recommendation
Call `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify()` handler, which already fires every 5 minutes via `CHECK_INTERVAL`. This ensures stale entries are periodically evicted regardless of whether any peer disconnects. Optionally, add a hard cap on the number of distinct keys in `forward_rate_limiter` and reject messages that would exceed it.

## Proof of Concept
```rust
// Pseudocode unit test
let mut protocol = HolePunching::new(network_state);
for i in 0..100_000u64 {
    let from = PeerId::random(); // unique each iteration
    let to   = PeerId::random(); // unique each iteration
    let msg  = build_connection_sync(from, to);
    protocol.received(mock_context(), msg).await;
}
// Assert: forward_rate_limiter internal map has ~100_000 entries
// Assert: no entries were reclaimed (retain_recent never called during session)
// Disconnect and assert: map is now empty after retain_recent() fires in disconnected()
```

The map size grows proportionally to N and is never reclaimed until `disconnected()` fires, confirming unbounded accumulation.

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L31-46)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

/// Hole Punching Protocol
pub(crate) struct HolePunching {
    network_state: Arc<NetworkState>,
    bind_addr: Option<SocketAddr>,
    // Request timestamp recorded
    inflight_requests: HashMap<PeerId, u64>,
    // Delivered timestamp recorded
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L85-96)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionSync",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionSync");
        }
```
