Audit Report

## Title
Unbounded Heap Growth via Unique `from` Peer IDs Bypassing `forward_rate_limiter` and `pending_delivered` Deduplication — (File: network/src/protocols/hole_punching/component/connection_request.rs)

## Summary
The `forward_rate_limiter` in `HolePunching` is keyed on `(content.from, content.to, msg_item_id)` where `content.from` is fully attacker-controlled and receives no cryptographic validation. Sending `ConnectionRequest` messages with a unique random `from` per message creates a new, never-rate-limited bucket on every call, causing unbounded growth in the `HashMapStateStore` that is only evicted on disconnect. Separately, the `pending_delivered` map is keyed on `from_peer_id` and accumulates one entry per unique `from` with no size bound, cleaned only every 5 minutes. Both vectors are reachable by any unauthenticated remote peer and can exhaust node memory.

## Finding Description

**Outer rate limiter** (`mod.rs` L95–107) is keyed on `(session_id, msg.item_id())` at 30 req/sec per session. This correctly throttles message rate but does not prevent the inner growth vectors.

**`forward_rate_limiter`** (`connection_request.rs` L132–143) is keyed on `(content.from.clone(), content.to.clone(), self.msg_item_id)`. The `from` field is parsed only for multihash validity (`connection_request.rs` L36–38); no cryptographic ownership proof is required. Each unique `from` value creates a fresh bucket in the `governor::state::keyed::HashMapStateStore` (`mod.rs` L31–35, L46). The 1-req/sec quota is never reached for any individual key, so `check_key` always returns `Ok`. `retain_recent()` is called only in `disconnected()` (`mod.rs` L66–68) and never in `notify()`, so for any long-lived session the store accumulates entries at up to 30/sec with no eviction.

**`pending_delivered`** (`connection_request.rs` L161–167) checks `self.protocol.pending_delivered.get(&from_peer_id)`. With a unique `from` per message, this always returns `None`, bypassing the `HOLE_PUNCHING_INTERVAL` (2-minute) cooldown. The unconditional insert at `connection_request.rs` L234–237 fires on every successful send. This path requires `to == self_peer_id` (`connection_request.rs` L145) and at least one valid TCP IPv4/IPv6 listen address (`connection_request.rs` L217–219). Cleanup runs only in `notify()` every `CHECK_INTERVAL` (5 minutes, `mod.rs` L25), retaining entries where `(now - t) < TIMEOUT` (5 minutes, `mod.rs` L28), meaning up to a full 5-minute window of insertions accumulates before any eviction.

## Impact Explanation

**`forward_rate_limiter` (unbounded for long-lived connections):** 30 entries/sec, never cleaned during session. Over 1 hour per session: ~108,000 entries × ~100 bytes ≈ ~10 MB/session/hr. With the maximum number of inbound sessions sustained over hours, this grows to gigabytes, causing an OOM crash of the CKB node. This matches the **High** impact: *"Vulnerabilities which could easily crash a CKB node."*

**`pending_delivered` (bounded per 5-minute window):** 30/sec × 300 sec = 9,000 entries per session per window, each holding a `PeerId` key and `Vec<Multiaddr>`. Across many sessions this contributes hundreds of MB per window, compounding the OOM risk.

## Likelihood Explanation

The attack requires only an unauthenticated TCP connection to the victim, knowledge of the victim's peer ID (publicly discoverable from the P2P network), and the ability to send `ConnectionRequest` messages with a fresh random `from` multihash per message at 30/sec. No PoW, no key material, and no victim interaction is needed. The `from` field is validated only for multihash byte format (`connection_request.rs` L36–38). The attack is trivially scriptable, repeatable, and sustainable indefinitely.

## Recommendation

1. **Key `forward_rate_limiter` on `(session_id, to, item_id)`** instead of `(from, to, item_id)`. `session_id` is bounded by the number of active connections; `from` is not.
2. **Call `self.forward_rate_limiter.retain_recent()` inside `notify()`** in addition to `disconnected()`, so stale entries are evicted every `CHECK_INTERVAL` regardless of connection lifetime.
3. **Bound `pending_delivered` by size**, not only by time. Introduce a `MAX_PENDING_DELIVERED` constant and reject or evict (LRU) insertions when the map is full.
4. **Add a per-session cap on `pending_delivered` insertions** to prevent a single session from filling the shared map at the full 30/sec rate.

## Proof of Concept

Connect to a live node and stream `ConnectionRequest` messages at 30/sec with `to=<victim_peer_id>` (publicly known) and a fresh random valid multihash `from` each time, with at least one valid TCP IPv4/IPv6 `listen_addr`. After 5 minutes, `pending_delivered` holds ~9,000 entries (never deduplicated). The `forward_rate_limiter` `HashMapStateStore` also holds ~9,000 entries and continues growing for the lifetime of the connection with no eviction. Monitor victim RSS: growth is linear and unbounded for `forward_rate_limiter`, and resets only every 5 minutes for `pending_delivered`. Sustaining multiple sessions over hours exhausts node memory and causes an OOM crash. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
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

**File:** network/src/protocols/hole_punching/mod.rs (L66-68)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
```

**File:** network/src/protocols/hole_punching/mod.rs (L172-174)
```rust
        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
```
