Audit Report

## Title
Unbounded `forward_rate_limiter` HashMap Growth via Missing `retain_recent()` in `notify()` — (`network/src/protocols/hole_punching/mod.rs`)

## Summary

The `forward_rate_limiter` in `HolePunching` is backed by `governor::state::keyed::HashMapStateStore<(PeerId, PeerId, u32)>`. The only call to `retain_recent()` on this limiter occurs in `disconnected()`. A persistent connection that never disconnects can insert an unbounded number of unique `(from, to, item_id)` keys into the HashMap at up to 30 entries/sec, because the periodic `notify()` handler (every 5 minutes) never prunes the limiter. This leads to unbounded memory growth and eventual OOM crash of the node.

## Finding Description

`forward_rate_limiter` is declared as a keyed `RateLimiter<(PeerId, PeerId, u32)>` backed by `HashMapStateStore`: [1](#0-0) 

Every forwarded `ConnectionSync`, `ConnectionRequest`, or `ConnectionRequestDelivered` message calls `check_key` with a key derived from wire-supplied `content.from`, `content.to`, and `msg_item_id`: [2](#0-1) [3](#0-2) [4](#0-3) 

`retain_recent()` is called **only** in `disconnected()`: [5](#0-4) 

The `notify()` handler (fired every `CHECK_INTERVAL` = 5 minutes) prunes `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on `forward_rate_limiter` or `rate_limiter`: [6](#0-5) 

The outer per-session rate limiter is keyed by `(session_id, msg_item_id)` and allows 30 req/sec: [7](#0-6) 

This outer limiter throttles the insertion rate but does not bound the number of distinct keys inserted into `forward_rate_limiter`. Since `from` and `to` are fully attacker-controlled PeerIds deserialized from the wire, each message with a fresh `(from, to)` pair inserts a new `HashMap` entry that is never evicted for the lifetime of the connection.

## Impact Explanation

Each unique `(PeerId, PeerId, u32)` key inserts a new entry into the `HashMapStateStore`. A `PeerId` is a multihash (~39 bytes), making each key ~82 bytes plus governor's `AtomicU64` state and `HashMap` overhead (~100–150 bytes/entry). At 30 entries/sec sustained over 24 hours, a single attacker connection produces ~2.6 million entries, consuming 300–400 MB. Multiple concurrent attackers multiply this linearly. The result is unbounded heap growth leading to OOM and node crash.

This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

Any unprivileged peer can open a TCP connection to a CKB node with HolePunching enabled. No proof-of-work, authentication, or special role is required. The attacker simply maintains the session and sends valid `ConnectionSync` (or `ConnectionRequest`/`ConnectionRequestDelivered`) messages with freshly generated random `from`/`to` PeerIds at ≤30/sec. The attack is trivially repeatable, requires no victim interaction, and is limited only by the attacker's ability to maintain a TCP connection.

## Recommendation

Add `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify()` handler, which already fires every 5 minutes (`CHECK_INTERVAL`). This ensures stale entries are evicted periodically regardless of connection lifetime, bounding the HashMap to entries active within the last rate-limiter window.

```rust
async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
    // existing cleanup
    self.pending_delivered.retain(|_, (_, t)| (now - *t) < TIMEOUT);
    self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
    // add these:
    self.rate_limiter.retain_recent();
    self.forward_rate_limiter.retain_recent();
    // ...
}
```

## Proof of Concept

1. Connect to a CKB node with HolePunching enabled.
2. In a loop at ≤30 messages/sec, send `ConnectionSync { from: random_peer_id_i, to: random_peer_id_j, route: [] }` where each iteration uses freshly generated random PeerIds.
3. Never disconnect.
4. After `N` seconds, `forward_rate_limiter`'s internal `HashMapStateStore` contains `N × 30` entries.
5. Observe: the `notify()` callback fires every 5 minutes but does not call `retain_recent()` on `forward_rate_limiter`, so no eviction occurs.
6. Monitor RSS of the CKB node process; it grows monotonically at ~4–5 KB/sec per attacker connection until OOM.

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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L134-145)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionRequestDelivered",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionRequestDelivered");
        }
```
