Audit Report

## Title
Unbounded `forward_rate_limiter` HashMap Growth via Unique `(PeerId, PeerId, u32)` Keys During Persistent Sessions — (`network/src/protocols/hole_punching/mod.rs`)

## Summary

The `forward_rate_limiter` in `HolePunching` is backed by `governor::state::keyed::HashMapStateStore<(PeerId, PeerId, u32)>`, which accumulates one entry per unique key and requires periodic pruning via `retain_recent()`. However, `retain_recent()` is called only in `disconnected()` and never in `notify()`. An attacker maintaining a persistent P2P session can send `ConnectionRequest`, `ConnectionRequestDelivered`, or `ConnectionSync` messages with unique `(from, to)` PeerId pairs at up to 30 per second, inserting a new HashMap entry per unique triple, causing unbounded memory growth that terminates the node via OOM.

## Finding Description

`HolePunching` declares two rate limiters backed by `HashMapStateStore`: [1](#0-0) 

The `forward_rate_limiter` uses `(PeerId, PeerId, u32)` as its key type, meaning the key space is unbounded — any syntactically valid pair of PeerIds constitutes a distinct key.

`retain_recent()` is called in exactly one place — `disconnected()`: [2](#0-1) 

The `notify()` handler (fired every 5 minutes via `CHECK_INTERVAL`) prunes `pending_delivered` and `inflight_requests` but **never calls `retain_recent()`** on either rate limiter: [3](#0-2) 

The exploit path is:

1. The attacker establishes a persistent P2P session with HolePunching enabled.
2. In `received()`, the session-level `rate_limiter` (keyed by `(session_id, msg_item_id)`) gates entry at 30 req/sec per `(session, msg_type)`: [4](#0-3) 
3. Each message that passes this gate is dispatched to a component handler. In `ConnectionRequestProcess::execute()`, after lightweight validation (format-only PeerId check, listen_addrs count, max_hops), `forward_rate_limiter.check_key()` is called with the attacker-controlled `(from, to, item_id)` triple: [5](#0-4) 
4. Each unique `(from, to, item_id)` triple inserts a new entry into the `HashMapStateStore`. The same pattern exists in `ConnectionRequestDeliveredProcess`: [6](#0-5) 
and `ConnectionSyncProcess`: [7](#0-6) 
5. PeerId validation only checks byte format, not registry membership: [8](#0-7) 
6. Since `retain_recent()` is never called in `notify()`, entries accumulate for the entire lifetime of the session. The `forward_rate_limiter` quota is 1/sec per key, so each unique `(from, to)` pair passes the forward check on first use and inserts a permanent entry until disconnect.

The `route.contains(self_peer_id)` check at line 128 of `connection_request.rs` is trivially bypassed by omitting the node's own peer ID from the route field.

## Impact Explanation

At 30 unique `(from, to)` pairs per second (bounded by the session-level `rate_limiter`), each entry consuming approximately 200 bytes (two PeerIds of ~39 bytes each + `u32` + HashMap overhead + governor internal state), the HashMap grows at ~6 KB/sec. Over a 24-hour session this accumulates to ~500 MB per attacker session. Multiple concurrent attacker sessions multiply this linearly. Sustained growth causes process-level OOM, crashing the CKB node.

This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

The attacker requires only: (1) a persistent P2P session with HolePunching enabled, and (2) the ability to craft protocol messages with arbitrary `from`/`to` PeerId byte values. Both are standard P2P capabilities requiring no privilege, no leaked keys, and no victim mistakes. PeerId bytes are validated for format only, so any syntactically valid multihash bytes are accepted. The attack is repeatable, requires no timing precision, and is effective from a single session.

## Recommendation

Call `self.rate_limiter.retain_recent()` and `self.forward_rate_limiter.retain_recent()` inside `notify()`, alongside the existing `pending_delivered` and `inflight_requests` cleanup. This ensures both HashMaps are pruned every `CHECK_INTERVAL` (5 minutes) regardless of whether any peer disconnects, bounding memory growth to at most `30 req/sec × 5 min × 60 sec = 9,000` entries per cleanup cycle.

## Proof of Concept

```rust
// Pseudocode unit/integration test
let mut hp = HolePunching::new(network_state);
// Simulate a persistent session — no disconnect occurs
for i in 0..108_000u64 {  // 30/sec × 3600 sec = 1 hour
    let from = PeerId::random();  // unique each iteration
    let to   = PeerId::random();  // unique each iteration
    let msg  = build_connection_request(from, to, valid_listen_addrs());
    hp.received(session_ctx, msg).await;
    // notify() fires every 5 min but never calls retain_recent()
}
// Assert: forward_rate_limiter internal HashMap has ~108,000 entries
// retain_recent() was never called; no disconnect occurred
// Memory consumed: ~21 MB and growing with no upper bound
// At 24h: ~2.6M entries, ~500 MB
```

Manual steps: connect to a CKB node with HolePunching enabled, send `ConnectionRequest` messages in a loop at 30/sec with randomly generated `from`/`to` PeerId bytes (valid multihash format), and monitor the node's RSS memory. Memory will grow monotonically at ~6 KB/sec with no plateau until the session is terminated.

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-40)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
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
