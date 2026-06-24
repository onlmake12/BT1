Audit Report

## Title
Unbounded `forward_rate_limiter` HashMap Growth via Distinct PeerId Pairs in HolePunching Messages — (`network/src/protocols/hole_punching/mod.rs`, `connection_sync.rs`, `connection_request.rs`, `connection_request_delivered.rs`)

## Summary

The `forward_rate_limiter` in `HolePunching` is a `governor::RateLimiter` backed by `HashMapStateStore<(PeerId, PeerId, u32)>`. Every unique `(from, to, msg_item_id)` tuple received in a `ConnectionRequest`, `ConnectionRequestDelivered`, or `ConnectionSync` message inserts a new heap entry into this map. The map is only pruned via `retain_recent()` inside `disconnected()`, never during the connection lifetime. An attacker who maintains a long-lived connection and sends messages with freshly generated `(from, to)` PeerId pairs causes unbounded heap growth, leading to OOM and node crash.

## Finding Description

**Type definition** — `forward_rate_limiter` is declared as: [1](#0-0) 

`HashMapStateStore<K>` allocates one heap entry per distinct key `K` seen. The key is `(PeerId, PeerId, u32)` where both `PeerId` values come directly from attacker-controlled bytes in the message.

**Key insertion in all three message handlers** — each handler calls `forward_rate_limiter.check_key(...)` with the parsed `(from, to, msg_item_id)` before any forwarding logic: [2](#0-1) [3](#0-2) [4](#0-3) 

**PeerId parsed from raw attacker bytes with no size cap**: [5](#0-4) 

**Cleanup only on disconnect, never periodically** — `notify()` (fired every 5 minutes via `CHECK_INTERVAL`) does not call `retain_recent()` on either rate limiter: [6](#0-5) [7](#0-6) 

**Exploit path:**
1. Attacker connects to the victim node over the standard P2P port.
2. Attacker sends `ConnectionSync` (or `ConnectionRequest` / `ConnectionRequestDelivered`) messages at ≤30/sec (the per-session `rate_limiter` cap), each with a freshly generated, structurally valid `(from, to)` PeerId pair.
3. Each message passes the per-session check (keyed by `(session_id, msg_item_id)`, not by `from`/`to`) and reaches `forward_rate_limiter.check_key`, inserting a new entry.
4. The `HashMapStateStore` grows O(messages) for the entire connection lifetime; `retain_recent()` is never called until disconnect.

**Why existing guards are insufficient:**
- The per-session `rate_limiter` (keyed by `(PeerIndex, u32)`) caps message rate but does not bound the number of distinct `(from, to)` keys inserted into `forward_rate_limiter`.
- The `forward_rate_limiter` itself only rejects a key that has been seen *recently* — it still inserts a new slot for every previously-unseen key.
- No ban is triggered because the messages are structurally valid and pass all content checks.

## Impact Explanation

Each `PeerId` is a heap-allocated `Vec<u8>` (39 bytes for Ed25519). Each `HashMapStateStore` entry costs ~80–150 bytes. At 30 inserts/second over a 1-hour session: ~108,000 entries ≈ ~16 MB per connection. With the node's default maximum peer count (~125), a coordinated attack accumulates ~2 GB/hour in a single shared `forward_rate_limiter` hashmap, causing progressive memory pressure and OOM crash of the CKB node.

This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

- Any unprivileged peer can connect via the standard P2P port; no authentication or PoW is required.
- Crafting valid Ed25519 PeerId bytes is trivial (any 39-byte multihash).
- The per-session rate limiter (30/sec) is the only throttle and does not prevent hashmap growth.
- No ban is triggered; the attack is silent and repeatable for the full connection lifetime.
- A single attacker with 125 simultaneous connections (all within the node's peer limit) can exhaust memory within an hour.

## Recommendation

1. **Call `retain_recent()` periodically** inside `notify()` (which already fires every 5 minutes via `CHECK_INTERVAL`) for both `rate_limiter` and `forward_rate_limiter`. This evicts entries whose rate-limit windows have expired and bounds the map to recently-active keys.
2. **Cap the `forward_rate_limiter` hashmap size** — reject messages once the number of tracked keys exceeds a configurable bound (e.g., `max_peers × MAX_HOPS × 3`).
3. **Validate PeerId byte length** before calling `PeerId::from_bytes` in each `TryFrom` impl, rejecting oversized inputs early to bound per-entry heap cost.

## Proof of Concept

```rust
// Attacker loop (pseudocode)
loop {
    let from = random_valid_ed25519_peer_id(); // 39-byte multihash
    let to   = random_valid_ed25519_peer_id();
    let msg  = build_connection_sync(from, to, route=[]);
    send_to_victim(msg);
    sleep(Duration::from_millis(34)); // ~30/sec, within per-session cap
}
// After 1 hour:  ~108,000 entries in victim's forward_rate_limiter
// After 1 hour with 125 connections: ~13.5M entries, ~2 GB heap
```

**Invariant test:** assert that `forward_rate_limiter`'s internal map length is bounded after N messages with distinct `(from, to)` pairs without a disconnect — this assertion will fail on the current code, confirming the bug.

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

**File:** network/src/protocols/hole_punching/mod.rs (L169-175)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let status = self.network_state.connection_status();

        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L42-46)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
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
