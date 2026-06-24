Audit Report

## Title
Unbounded `forward_rate_limiter` HashMap Growth via Distinct PeerId Pairs in HolePunching Messages — (`network/src/protocols/hole_punching/mod.rs`)

## Summary

The `forward_rate_limiter` field in `HolePunching` is a `governor::RateLimiter` backed by a `HashMapStateStore<(PeerId, PeerId, u32)>`. Every unique `(from, to, msg_item_id)` tuple seen in a forwarded message inserts a new heap entry. Because `retain_recent()` is called only inside `disconnected()` and never inside `notify()`, an attacker who holds a long-lived connection and sends messages with freshly generated `(from, to)` PeerId pairs causes the hashmap to grow without bound for the entire duration of the session, leading to progressive memory exhaustion and potential OOM crash of the node.

## Finding Description

**Type definition** — `forward_rate_limiter` is declared as a `RateLimiter<(PeerId, PeerId, u32)>` backed by `HashMapStateStore`, which allocates one slot per distinct key and never evicts entries autonomously: [1](#0-0) 

**Insertion path** — All three message handlers call `forward_rate_limiter.check_key()` with attacker-controlled `(from, to)` pairs parsed directly from message bytes via `PeerId::from_bytes`, with no length cap beyond what `PeerId::from_bytes` accepts: [2](#0-1) [3](#0-2) [4](#0-3) 

**Cleanup only on disconnect** — `retain_recent()` is called exclusively in `disconnected()`: [5](#0-4) 

**`notify()` does not call `retain_recent()`** — the periodic callback (fires every `CHECK_INTERVAL` = 5 minutes via `CHECK_TOKEN`) only prunes `pending_delivered` and `inflight_requests`, leaving both rate-limiter hashmaps untouched: [6](#0-5) 

**Per-session rate limiter is insufficient** — the `rate_limiter` check caps throughput at 30 msg/sec per `(session_id, msg_type)` but does not prevent new `(from, to)` keys from being inserted into `forward_rate_limiter` at that same rate. The `forward_rate_limiter` quota is 1 msg/sec per `(from, to, msg_item_id)`, so every fresh pair passes the check and inserts a new entry: [7](#0-6) 

## Impact Explanation

Each `PeerId` is a heap-allocated `Vec<u8>` (~39 bytes for Ed25519). Each `HashMapStateStore` entry costs roughly 80–150 bytes (two `PeerId` vecs + `u32` + `hashbrown` slot overhead). At 30 insertions/second over a 1-hour session: ~108,000 entries ≈ ~16 MB per connection. With a typical max peer count of 125 connections under sustained attack: ~2 GB/hour in a single hashmap, causing OOM and node crash.

This matches the allowed bounty impact: **High — Vulnerabilities which could easily crash a CKB node** (10001–15000 points).

## Likelihood Explanation

- Any unprivileged peer can connect via the standard P2P port.
- Crafting structurally valid `PeerId` bytes (39-byte Ed25519 multihash) is trivial.
- The 30 msg/sec per-session cap is the only throttle; it does not prevent hashmap growth, only bounds the insertion rate.
- No PoW, authentication, or ban is triggered: messages are structurally valid and pass all content checks.
- The attack is repeatable across all three message types (`ConnectionSync`, `ConnectionRequest`, `ConnectionRequestDelivered`), each contributing to the same shared `forward_rate_limiter`.

## Recommendation

1. **Call `retain_recent()` periodically** inside `notify()` for both `rate_limiter` and `forward_rate_limiter`. The existing `CHECK_INTERVAL` (5 minutes) timer already fires via `CHECK_TOKEN`; adding two `retain_recent()` calls there is sufficient to evict expired entries.
2. **Cap the `forward_rate_limiter` hashmap size** — reject messages once tracked key count exceeds a configurable bound (e.g., `max_peers × MAX_HOPS × 3`).
3. **Validate PeerId byte length** before calling `PeerId::from_bytes` in `SyncContent::try_from`, `RequestContent::try_from`, and `DeliverdContent::try_from`, rejecting oversized inputs early to bound per-entry heap cost.

## Proof of Concept

```rust
// Attacker loop (pseudocode)
loop {
    let from = random_valid_ed25519_peer_id(); // 39-byte multihash
    let to   = random_valid_ed25519_peer_id(); // distinct each iteration
    let msg  = build_connection_sync(from, to, route=[]);
    send_to_victim(msg);
    sleep(Duration::from_millis(34)); // ~30/sec, within per-session cap
}
// After 1 hour:  ~108,000 entries in victim's forward_rate_limiter
// After 1 hour with 125 connections: ~13.5M entries, ~2 GB heap
// retain_recent() is never called while the session is live → O(messages) growth
```

A unit test can confirm the invariant: create a `HolePunching` instance, call `received()` 10,000 times with distinct `(from, to)` pairs without calling `disconnected()`, and assert that the `HashMapStateStore` length equals 10,000 (never shrinks).

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
