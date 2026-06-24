Audit Report

## Title
Unbounded `forward_rate_limiter` HashMapStateStore Growth via Attacker-Controlled `(from, to)` Keys — (`network/src/protocols/hole_punching/mod.rs`)

## Summary
The `HolePunching` protocol's `forward_rate_limiter` is backed by `governor::state::keyed::HashMapStateStore<(PeerId, PeerId, u32)>`, which requires periodic `retain_recent()` calls to evict stale entries. The only call site is `disconnected()`, meaning the map grows without bound for the entire lifetime of a live session. An unprivileged attacker can insert up to 30 new distinct keys per second by sending messages with unique attacker-chosen `from`/`to` PeerId pairs, leading to unbounded heap growth and eventual node OOM crash.

## Finding Description
`forward_rate_limiter` is declared as `RateLimiter<(PeerId, PeerId, u32)>` backed by `HashMapStateStore`: [1](#0-0) 

`retain_recent()` is called **only** in `disconnected()`: [2](#0-1) 

The `notify()` handler (fired every 5 minutes via `CHECK_INTERVAL`) cleans `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on either rate limiter: [3](#0-2) 

Every `ConnectionSync` message calls `forward_rate_limiter.check_key(&(content.from, content.to, self.msg_item_id))`, where `from` and `to` are parsed directly from the wire with no constraint that they correspond to known or connected peers: [4](#0-3) 

The outer `rate_limiter` (keyed by `(session_id, item_id)`, 30 req/s) throttles throughput but does not bound the total number of distinct `(from, to)` keys inserted into `forward_rate_limiter` over a long-lived connection. The same pattern applies to `ConnectionRequest` and `ConnectionRequestDelivered`, which also call `forward_rate_limiter`: [5](#0-4) [6](#0-5) 

## Impact Explanation
At 30 messages/second, a 24-hour session produces ~2.6 million unique `(PeerId, PeerId, u32)` keys. Each entry holds two `PeerId` values (~39 bytes each) plus governor state and `HashMap` overhead — roughly 200–300 bytes per entry — yielding ~500–750 MB of heap growth from a single attacker session. Multiple concurrent sessions multiply the effect. Memory exhaustion crashes the node process. This maps to **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
The attacker requires only a standard P2P connection — no special privileges, no PoW, no key material. Maintaining a long-lived TCP session and sending 30 crafted messages per second is trivially achievable. The `from`/`to` PeerIds require only valid byte-length encoding; no signature or proof of ownership is checked before the rate-limiter key is inserted.

## Recommendation
1. Call `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the existing `notify()` handler alongside the existing `pending_delivered`/`inflight_requests` cleanup.
2. Alternatively, validate that `from` equals the sending session's peer ID before inserting into `forward_rate_limiter`, eliminating the ability to inject arbitrary keys.
3. Consider switching to a fixed-capacity LRU-based rate-limiter store to provide a hard memory ceiling.

## Proof of Concept
```
1. Connect to a victim CKB node as a normal P2P peer.
2. In a loop at 30 msg/s, send ConnectionSync messages where each message has:
     from = random_peer_id_i   (unique per message, valid byte-length encoding)
     to   = random_peer_id_j   (unique per message)
     route = []                (empty, passes the MAX_HOPS check)
3. Maintain the session for several hours without disconnecting.
4. Observe victim node RSS growing proportionally to messages sent (~200-300 bytes per message).
5. Node OOMs and crashes; no disconnect occurs until the attacker chooses.
```

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

**File:** network/src/protocols/hole_punching/mod.rs (L169-176)
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L1-1)
```rust
use std::borrow::Cow;
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L1-1)
```rust
use std::{borrow::Cow, net::SocketAddr};
```
