The code is fully readable. Let me now analyze the exact vulnerability path.

**Key facts from the code:**

1. `forward_rate_limiter` is `RateLimiter<(PeerId, PeerId, u32)>` backed by `HashMapStateStore<(PeerId, PeerId, u32)>` — keyed by arbitrary attacker-controlled PeerId pairs. [1](#0-0) 

2. `ConnectionSyncProcess::execute()` calls `forward_rate_limiter.check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))` — `from` and `to` are decoded directly from the wire message with no constraint on their values. [2](#0-1) 

3. `retain_recent()` is called **only** in `disconnected()`, never in `notify()`. [3](#0-2) 

4. The `notify()` handler (fired every `CHECK_INTERVAL = 5 minutes`) cleans up `pending_delivered` and `inflight_requests` but **does not** call `retain_recent()` on either rate limiter. [4](#0-3) 

5. The per-session gate (`rate_limiter` keyed by `(PeerIndex, u32)`) allows 30 req/s per session — this is the only upstream throttle before `forward_rate_limiter.check_key()` is reached. [5](#0-4) 

**The attack path is concrete:**

- A single unprivileged peer maintains one long-lived HolePunching session.
- It sends ConnectionSync messages at ≤30/s, each carrying a fresh unique `(from, to)` PeerId pair.
- Each call to `check_key()` on a never-seen key inserts a new entry into the `HashMapStateStore` and succeeds (quota not yet consumed for that key).
- The `forward_rate_limiter` map grows at up to 30 entries/second with no reclamation until the session disconnects.
- After 1 hour: ~108,000 entries; after 24 hours: ~2.6M entries. Each entry holds two `PeerId` values (~39 bytes each) plus a `u32` plus HashMap overhead — roughly 150–200 bytes per entry → ~400–500 MB/day from a single session.

**Why the existing guards do not prevent this:**

- The per-session `rate_limiter` limits message *rate* but not the *cardinality* of keys in `forward_rate_limiter`.
- `notify()` at 5-minute intervals does not call `retain_recent()`.
- `governor`'s `HashMapStateStore` does not self-evict; it only shrinks when `retain_recent()` is explicitly called.
- There is no cap on the number of distinct `(PeerId, PeerId, u32)` keys the map may hold.

---

### Title
Unbounded `forward_rate_limiter` HashMapStateStore growth via unique (from, to) PeerId pairs in ConnectionSync messages — (`network/src/protocols/hole_punching/mod.rs`)

### Summary
A single unprivileged remote peer can cause unbounded heap growth in the `HolePunching` protocol handler by sending a continuous stream of `ConnectionSync` messages, each carrying a distinct `(from, to)` PeerId pair. The `forward_rate_limiter`'s `HashMapStateStore` accumulates one new entry per unique pair and is never reclaimed during the session lifetime because `retain_recent()` is only invoked in `disconnected()` and the periodic `notify()` handler omits it.

### Finding Description
`HolePunching::forward_rate_limiter` is a `governor::RateLimiter` backed by `HashMapStateStore<(PeerId, PeerId, u32)>`. Every call to `check_key(&(from, to, msg_item_id))` with a previously unseen key allocates a new map entry. The `from` and `to` fields are decoded directly from the wire-level `ConnectionSync` message with no constraint on their values beyond being valid PeerId bytes. The per-session `rate_limiter` (keyed by `(PeerIndex, u32)`) throttles the session to 30 messages/second but does not bound the number of distinct `(PeerId, PeerId, u32)` keys that can be inserted into `forward_rate_limiter`. The `notify()` handler, which fires every 5 minutes, cleans up `pending_delivered` and `inflight_requests` but never calls `retain_recent()` on either rate limiter. Reclamation only occurs in `disconnected()`, which the attacker controls by keeping the session alive.

### Impact Explanation
Unbounded heap growth in the `forward_rate_limiter` map. At 30 entries/second, a single session accumulates ~108,000 entries per hour and ~2.6 million entries per 24 hours. At ~150–200 bytes per entry (two PeerId values + u32 + HashMap overhead), this is ~400–500 MB/day from one session. Multiple concurrent sessions multiply the effect. Sustained over hours, this degrades node performance and can exhaust available memory, causing the node to crash or become unresponsive.

### Likelihood Explanation
The attack requires only a standard P2P connection to the HolePunching protocol — no privileges, no PoW, no key material. The attacker simply generates fresh random PeerId bytes for `from` and `to` in each message. The per-session rate cap of 30 req/s is the only throttle, and it does not prevent the attack; it only sets the rate of growth. The session can be maintained indefinitely.

### Recommendation
Call `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify()` handler, which already fires every 5 minutes via `CHECK_INTERVAL`. This ensures stale entries are periodically evicted regardless of whether any peer disconnects. Optionally, add a hard cap on the number of keys in `forward_rate_limiter` and reject messages that would exceed it.

### Proof of Concept
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
// Assert: no entries were reclaimed (retain_recent never called)
// Disconnect and assert: map is now empty after retain_recent()
```

The map size grows proportionally to N and is never reclaimed until `disconnected()` fires, confirming the unbounded accumulation. [4](#0-3) [3](#0-2) [2](#0-1)

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
