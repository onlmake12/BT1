Audit Report

## Title
Unbounded `forward_rate_limiter` HashMapStateStore Growth via Unique Attacker-Controlled `(PeerId, PeerId)` Keys — (`network/src/protocols/hole_punching/mod.rs`)

## Summary

The `forward_rate_limiter` in `HolePunching` uses a `HashMapStateStore` keyed by `(PeerId, PeerId, u32)`, where the first two fields are parsed directly from attacker-controlled message content. The `retain_recent()` cleanup is only called in `disconnected()`, never in the `notify()` timer. An attacker maintaining one persistent session can insert unbounded unique keys into the store, causing linear memory growth with no eviction until disconnect.

## Finding Description

`HolePunching` declares two rate limiters backed by `governor::state::keyed::HashMapStateStore`: [1](#0-0) 

The `notify()` handler fires every 5 minutes and cleans `pending_delivered` and `inflight_requests`, but never calls `retain_recent()` on either rate limiter: [2](#0-1) 

`retain_recent()` is only called inside `disconnected()`: [3](#0-2) 

In `ConnectionRequestProcess::execute()`, the `forward_rate_limiter` is checked with a key composed of `content.from`, `content.to`, and `self.msg_item_id`: [4](#0-3) 

Both `content.from` and `content.to` are parsed directly from the attacker-supplied message body with no constraint beyond being valid PeerId bytes: [5](#0-4) 

`msg_item_id` is a fixed discriminant for the `ConnectionRequest` message type, so the effective key space is `(attacker_from, attacker_to, FIXED_CONST)`. The outer `rate_limiter` is keyed by `(session_id, msg.item_id())` and caps throughput at 30 req/sec per session: [6](#0-5) 

This 30 req/sec cap limits insertion rate but does not bound total growth — each unique `(from, to)` pair inserts a new entry that is never evicted while the session remains open. The `governor` crate's `HashMapStateStore` does not auto-evict stale entries; that is the explicit purpose of `retain_recent()`, which is absent from the timer path.

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node.**

At 30 unique `(from, to)` pairs per second, with each entry consuming approximately 100–150 bytes (two 39-byte PeerIds + key overhead + rate limiter state), the store grows at roughly 3–4.5 KB/sec. Over 24 hours this accumulates to ~260–390 MB; over several days on a memory-constrained node, this causes OOM and process termination. The growth is deterministic and unbounded for the lifetime of any single session. No remote code execution is possible; impact is local node crash.

## Likelihood Explanation

- HolePunching must be enabled (opt-in via `support_protocols` config), which reduces the default attack surface.
- Beyond that, the attacker requires only one persistent P2P connection — no elevated privileges.
- Generating unique PeerIds is trivial (random 32-byte seeds hashed into multihash format).
- The attack is slow (OOM in days, not seconds), but the invariant is definitively broken: there is no mechanism to bound or evict `forward_rate_limiter` entries during an active session.
- The attack is repeatable and requires no victim interaction.

## Recommendation

Add `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify()` handler, alongside the existing `pending_delivered` and `inflight_requests` cleanup: [2](#0-1) 

This ensures stale entries are evicted every 5 minutes regardless of whether a session disconnects, bounding the store size to entries active within the last rate-limit window.

## Proof of Concept

1. Enable HolePunching on a test node via `support_protocols`.
2. Establish one persistent P2P session to the target node.
3. Send `ConnectionRequest` messages at 30/sec, each with a freshly generated random `from` and `to` PeerId (valid multihash bytes, e.g., random 39-byte sequences).
4. After N seconds, the `forward_rate_limiter` `HashMapStateStore` will contain exactly N × 30 entries with zero evictions (session remains open; `notify()` fires but does not call `retain_recent()`).
5. Monitor RSS of the `ckb` process — growth will be linear at ~4 KB/sec.
6. Confirm that calling `retain_recent()` manually (or patching `notify()` to call it) immediately drops the store to only recently-active entries.

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
