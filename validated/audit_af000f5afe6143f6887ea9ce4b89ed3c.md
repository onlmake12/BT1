Audit Report

## Title
Unbounded Memory Growth in `HolePunching` `forward_rate_limiter` via Attacker-Controlled PeerIds - (File: `network/src/protocols/hole_punching/mod.rs`)

## Summary

The `HolePunching` protocol's `forward_rate_limiter` is backed by an unbounded `HashMapStateStore` and keyed by attacker-controlled `(content.from, content.to, msg_item_id)` triples deserialized directly from the message payload. Because `retain_recent()` is only invoked on peer disconnect and never in the periodic `notify()` handler, a single connected peer can continuously insert new entries into the limiter's backing `HashMap` by sending messages with unique `from`/`to` PeerId pairs, growing node memory without bound until OOM.

## Finding Description

`forward_rate_limiter` is declared as `RateLimiter<(PeerId, PeerId, u32)>` backed by `governor::state::keyed::HashMapStateStore`: [1](#0-0) 

The `from` and `to` fields are parsed directly from the raw message payload in all three processors:
- `connection_request.rs` L36–40 [2](#0-1) 
- `connection_request_delivered.rs` L38–42 [3](#0-2) 
- `connection_sync.rs` L42–46 [4](#0-3) 

Each call to `forward_rate_limiter.check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))` inserts a new entry into the `HashMapStateStore` for every previously-unseen triple: [5](#0-4) [6](#0-5) [7](#0-6) 

The outer `rate_limiter` (keyed by `(session_id, item_id)`, quota 30/sec) gates access before the `forward_rate_limiter` check, but it is bounded by the number of active sessions and does not prevent injection of arbitrary `(from, to)` keys: [8](#0-7) 

`retain_recent()` is called on both limiters only in `disconnected()`: [9](#0-8) 

The `notify()` handler (fires every 5 minutes via `CHECK_INTERVAL`) prunes `pending_delivered` and `inflight_requests` but **never calls `retain_recent()` on either rate limiter**: [10](#0-9) 

As long as the attacker maintains the TCP connection, the `HashMapStateStore` grows indefinitely. The `forward_rate_limiter` quota is 1/sec per key, but each new `(from, to)` pair is a new key and always passes, so the rate limit provides no protection against key-space exhaustion: [11](#0-10) 

## Impact Explanation

**High (10001–15000 points): Vulnerabilities which could easily crash a CKB node.**

At 30 messages/sec per message type across 3 message types, an attacker inserts up to 90 new `HashMapStateStore` entries per second. Each entry holds two `PeerId` values (~39 bytes each), a `u32`, and `HashMap` overhead (~150–200 bytes total). Over a sustained 24-hour connection: 90 × 86,400 ≈ 7.8M entries ≈ 1.2–1.5 GB of heap growth from a single peer. This exhausts available memory, triggering OOM conditions that crash or severely degrade the node, preventing block processing, transaction relay, and RPC service.

## Likelihood Explanation

Any unprivileged peer that can establish a TCP connection to a CKB node with `HolePunching` enabled can trigger this. No tokens, keys, or special privileges are required. The attacker only needs to send structurally valid `ConnectionRequest` messages (valid listen_addrs count 1–24, valid max_hops ≤ 6, valid route length ≤ 6) with freshly generated random `from`/`to` PeerId byte strings. The attack rate (30 msg/sec) is well within normal network capacity. The node has no periodic cleanup mechanism to bound the limiter's memory growth during an active connection.

## Recommendation

1. **Add `retain_recent()` calls in `notify()`**: The `notify()` handler already fires every 5 minutes (`CHECK_INTERVAL`). Add `self.rate_limiter.retain_recent()` and `self.forward_rate_limiter.retain_recent()` inside it to periodically evict stale entries.
2. **Validate `from` against the sending session**: Before calling `forward_rate_limiter.check_key()`, verify that `content.from` matches the actual `PeerId` of the sending session. Messages where `from` does not match the session's peer ID should be rejected or banned before reaching the rate limiter, eliminating the ability to inject arbitrary keys.
3. **Use a bounded state store**: Replace `HashMapStateStore` for `forward_rate_limiter` with a capacity-bounded LRU-backed store to cap worst-case memory usage regardless of cleanup timing.

## Proof of Concept

1. Attacker establishes a single P2P connection to a target CKB node with `HolePunching` enabled.
2. Attacker sends `ConnectionRequest` messages at 30/sec. Each message contains a freshly generated random `from` PeerId and `to` PeerId (arbitrary valid multihash byte strings), with 1–24 valid listen addresses, `max_hops` ≤ 6, and an empty route.
3. Each message passes the outer `rate_limiter` check (keyed by `(session_id, item_id)`, L95–107 of `mod.rs`) and reaches `forward_rate_limiter.check_key(&(from, to, item_id))` (L132–135 of `connection_request.rs`), inserting a new entry because the `(from, to)` pair has never been seen before.
4. The `notify()` handler fires every 5 minutes but never calls `retain_recent()` on `forward_rate_limiter` (L169–175 of `mod.rs`), so no entries are ever evicted during the connection.
5. After 24 hours: ~2.6M entries from `ConnectionRequest` alone (plus entries from `ConnectionRequestDelivered` and `ConnectionSync`), consuming ~500MB–1.5GB of heap.
6. Node OOM-kills or becomes unresponsive; all users of that node lose access to transaction submission, block relay, and RPC.

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

**File:** network/src/protocols/hole_punching/mod.rs (L255-257)
```rust
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-40)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L132-135)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L38-42)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L134-137)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L42-46)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L85-88)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
```
