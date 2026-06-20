The code is fully readable. Here is my analysis:

**Key facts confirmed from the source:**

1. `forward_rate_limiter` is typed as `RateLimiter<(PeerId, PeerId, u32)>` backed by `governor::state::keyed::HashMapStateStore<(PeerId, PeerId, u32)>` — a plain `HashMap` with no internal eviction. [1](#0-0) 

2. `retain_recent()` is called **only** in `disconnected()`, not in `notify()`. [2](#0-1) 

3. `notify()` (fired every 5 minutes via `CHECK_INTERVAL`) cleans `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on either rate limiter. [3](#0-2) 

4. The per-session gate (`rate_limiter`) is keyed by `(session_id, item_id)`. `item_id` for `ConnectionRequest` is always `0`, so this allows exactly 30 messages/second per session — each of which can carry a **distinct** `(from, to)` pair. [4](#0-3) 

5. Each `ConnectionRequest` with a unique `(from, to)` pair calls `forward_rate_limiter.check_key(&(from, to, 0))`, inserting a new entry into the `HashMapStateStore`. [5](#0-4) 

**Assessment:**

The `forward_rate_limiter`'s `HashMapStateStore` grows at up to 30 entries/second per attacker session. Each entry holds two `PeerId` values (~39 bytes each) plus HashMap overhead (~150–200 bytes total per entry). Without a disconnect, `retain_recent()` is never called, so the map accumulates indefinitely. Over 24 hours from a single connection: ~500 MB. With multiple attacker connections (up to the node's peer limit), the rate multiplies proportionally.

The `rate_limiter` (keyed by `(PeerIndex, u32)`) is **not** affected because `PeerIndex` is bounded by the number of active sessions — only `forward_rate_limiter` is unbounded.

---

### Title
Unbounded `forward_rate_limiter` HashMapStateStore growth via long-lived HolePunching session — (`network/src/protocols/hole_punching/mod.rs`)

### Summary
The `forward_rate_limiter` in `HolePunching` uses a `governor::HashMapStateStore` keyed by `(PeerId, PeerId, u32)`. Its `retain_recent()` cleanup is only triggered in `disconnected()`. An attacker who maintains a persistent connection and sends `ConnectionRequest` messages with unique `(from, to)` PeerId pairs at the allowed rate of 30/s causes unbounded growth of this map, eventually exhausting node memory.

### Finding Description
`HolePunching::forward_rate_limiter` is declared as `RateLimiter<(PeerId, PeerId, u32)>` backed by `HashMapStateStore`, which is a plain `HashMap` with no automatic eviction. The `governor` crate requires the caller to invoke `retain_recent()` periodically to purge stale entries. In this implementation, `retain_recent()` is called only inside `disconnected()` (line 68). The `notify()` handler, which fires every 5 minutes, cleans `pending_delivered` and `inflight_requests` but omits `retain_recent()` on both rate limiters. An attacker who never disconnects therefore prevents any cleanup from occurring. Each `ConnectionRequest` with a unique `(from, to)` pair passes the per-session gate (30/s for `item_id=0`) and inserts a new key into `forward_rate_limiter` via `check_key()`. Since PeerId values are attacker-controlled message fields, the key space is effectively unbounded.

### Impact Explanation
Memory grows at ~30 entries × ~200 bytes = ~6 KB/second per attacker connection. A single connection running for 24 hours accumulates ~500 MB. With multiple connections (up to the node's configured peer limit), the rate scales linearly. Sustained over days, this exhausts available RAM and causes an OOM crash of the CKB node process, constituting a remote denial-of-service.

### Likelihood Explanation
HolePunching is a production protocol enabled by default. Establishing a single P2P connection requires no privileges. The attacker only needs to maintain the connection and send valid `ConnectionRequest` messages at 30/s with unique `(from, to)` pairs — a trivially scriptable operation. No PoW, no key material, and no special network position is required.

### Recommendation
Call `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify()` handler in `network/src/protocols/hole_punching/mod.rs`, so stale entries are purged every 5 minutes regardless of whether any peer disconnects. [3](#0-2) 

### Proof of Concept
1. Connect to a CKB node with HolePunching enabled.
2. In a loop at 30 messages/second, send `ConnectionRequest` messages where `from` and `to` are freshly generated random `PeerId` values each iteration, with a valid `listen_addrs` entry and `max_hops ≤ 6`.
3. Never disconnect.
4. Observe `forward_rate_limiter`'s internal `HashMapStateStore` growing by 30 entries/second with no upper bound.
5. After sufficient time (hours to days depending on available RAM), the node process is killed by the OOM killer.

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
