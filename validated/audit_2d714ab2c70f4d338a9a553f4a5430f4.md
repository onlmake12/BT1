The code confirms this is a real vulnerability. Let me lay out the exact findings.

**Key facts from the code:**

1. `forward_rate_limiter` is typed as `RateLimiter<(PeerId, PeerId, u32)>` backed by `governor::HashMapStateStore`. [1](#0-0) 

2. The `from` and `to` fields in the key come directly from the attacker-controlled message payload, not from the session. [2](#0-1) 

3. `retain_recent()` is called **only** in `disconnected()`. The `notify()` handler (fires every 5 minutes) cleans up `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on either rate limiter. [3](#0-2) [4](#0-3) 

4. The outer `rate_limiter` (keyed by `(session_id, msg.item_id())`) allows 30 req/s per session — this is the insertion rate cap into `forward_rate_limiter`. [5](#0-4) 

5. The `forward_rate_limiter` quota is **1 req/s** (not 30 as the question states), but this is irrelevant to the growth: every call with a **new** key always succeeds on first use, inserting a new entry. [6](#0-5) 

---

### Title
Unbounded `forward_rate_limiter` HashMap growth via attacker-controlled `(from, to)` PeerId keys without periodic eviction — (`network/src/protocols/hole_punching/mod.rs`)

### Summary
An unprivileged remote peer can cause unbounded memory growth in `HolePunching::forward_rate_limiter` by sending `ConnectionRequest` (or `ConnectionRequestDelivered`, `ConnectionSync`) messages with unique attacker-chosen `(from, to)` PeerId pairs. Each unique pair inserts a new entry into the `governor::HashMapStateStore`. The only eviction call, `retain_recent()`, is placed exclusively in `disconnected()` and is never invoked by the periodic `notify()` handler, so a persistent session accumulates entries indefinitely.

### Finding Description
`HolePunching` maintains two rate limiters:
- `rate_limiter: RateLimiter<(PeerIndex, u32)>` — keyed by session ID, bounded by the number of active sessions.
- `forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>` — keyed by `(content.from, content.to, msg_item_id)` where `from` and `to` are arbitrary bytes from the message payload.

The outer `rate_limiter` caps message processing at 30 req/s per session. Each processed message then calls `forward_rate_limiter.check_key(&(content.from, content.to, msg_item_id))`. Because `from` and `to` are attacker-supplied, the attacker rotates them to always present a fresh key, guaranteeing a new HashMap entry on every call.

`retain_recent()` is called only in `disconnected()`. The `notify()` handler, which fires every 5 minutes, performs cleanup of `pending_delivered` and `inflight_requests` but contains no call to `retain_recent()` on either rate limiter. This means entries in `forward_rate_limiter` accumulate for the entire lifetime of a session.

### Impact Explanation
- Per session: 30 entries/second × ~150 bytes/entry ≈ 4.5 KB/s → ~390 MB/day → ~2.7 GB/week.
- With multiple malicious sessions (up to the node's max peer count, typically 125): growth scales linearly.
- The `governor::HashMapStateStore` has no internal capacity cap; it grows until the process is OOM-killed.

### Likelihood Explanation
Any peer that can establish a HolePunching protocol session can trigger this. No special privileges, PoW, or key material are required. The attacker only needs to maintain a persistent connection and send valid `ConnectionRequest` messages with rotating `from`/`to` PeerId bytes. The attack is slow but persistent and requires no reconnection.

### Recommendation
1. Call `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify()` handler, which already fires every 5 minutes.
2. Alternatively, cap the key space by keying `forward_rate_limiter` on `(SessionId, u32)` instead of `(PeerId, PeerId, u32)`, making it bounded by the number of active sessions just like `rate_limiter`.

### Proof of Concept
Inject N `ConnectionRequest` messages from one session, each with a unique `(from, to)` PeerId pair (e.g., incrementing byte sequences), without disconnecting. Assert that `forward_rate_limiter`'s internal HashMap size grows proportionally to N (capped at 30 per second by the outer limiter). Confirm no shrinkage occurs until `disconnected()` is called. Compare heap allocation before and after via `jemalloc` stats or a heap profiler.

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

**File:** network/src/protocols/hole_punching/mod.rs (L255-257)
```rust
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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
