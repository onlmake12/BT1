The vulnerability is real and confirmed. Here is the full analysis:

---

### Title
Unbounded `forward_rate_limiter` HashMap Growth via Persistent Connection with Unique PeerId Pairs — (`network/src/protocols/hole_punching/mod.rs`)

### Summary

The `HolePunching` protocol's `forward_rate_limiter` uses a `governor::RateLimiter` backed by a `HashMapStateStore<(PeerId, PeerId, u32)>`. Its cleanup method `retain_recent()` is called **only** in the `disconnected()` handler. The periodic `notify()` handler (fired every 5 minutes) cleans up `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on `forward_rate_limiter`. An attacker who maintains a persistent connection and sends `ConnectionRequest` messages with unique attacker-controlled `(from, to)` PeerId pairs causes the HashMap to grow without bound, eventually exhausting heap memory.

### Finding Description

**Type declaration:**

`forward_rate_limiter` is declared as `RateLimiter<(PeerId, PeerId, u32)>` backed by `HashMapStateStore`: [1](#0-0) 

**Cleanup only in `disconnected()`:**

`retain_recent()` is called on `forward_rate_limiter` only when a peer disconnects: [2](#0-1) 

**`notify()` does not clean `forward_rate_limiter`:**

The periodic timer handler cleans `pending_delivered` and `inflight_requests` but omits `forward_rate_limiter`: [3](#0-2) 

**Per-session rate limiter does not bound unique keys:**

The outer `rate_limiter` is keyed by `(PeerIndex, msg.item_id())` — a single fixed key per session per message type — limiting throughput to 30 messages/sec but placing no cap on the number of distinct `(from, to)` pairs that reach `forward_rate_limiter`: [4](#0-3) 

**Each unique pair inserts a new HashMap entry:**

Every `ConnectionRequest` with a novel `(from, to)` pair calls `check_key` on `forward_rate_limiter`, inserting a new entry into the `HashMapStateStore`: [5](#0-4) 

The `from` and `to` fields are fully attacker-controlled bytes in the message payload: [6](#0-5) 

### Impact Explanation

- Growth rate: 30 entries/second per persistent connection (capped by per-session rate limiter).
- Each entry stores two `PeerId` values (~39 bytes each), a `u32`, and governor internal state plus HashMap overhead — approximately 150–200 bytes per entry.
- After 24 hours on a single connection: ~2.6 million entries ≈ ~400 MB.
- Multiple simultaneous attacker connections multiply the rate linearly.
- No automatic eviction occurs until the attacker disconnects; the node has no way to force cleanup of `forward_rate_limiter` while the session is live.
- Result: heap exhaustion → OOM → node crash.

### Likelihood Explanation

- Requires only one persistent inbound P2P connection — no special privileges, no PoW, no keys.
- PeerId values in `from`/`to` fields are not validated against any connected peer registry; arbitrary bytes are accepted as long as they parse as valid multihash PeerIds.
- The attack is slow (hours to days for a single connection) but reliable and amplifiable with multiple connections.
- No existing guard prevents it: the per-session rate limiter bounds rate but not total unique keys; `notify()` does not call `retain_recent()`.

### Recommendation

Add `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify()` handler in `network/src/protocols/hole_punching/mod.rs`. This ensures periodic eviction of stale entries regardless of whether peers disconnect, bounding the HashMap to entries active within the last rate-limit window. [3](#0-2) 

### Proof of Concept

1. Connect one persistent TCP session to a victim CKB node's HolePunching protocol endpoint.
2. In a loop, construct `ConnectionRequest` messages where `from` = `PeerId::random()` and `to` = `PeerId::random()` (unique per iteration), with valid `listen_addrs` and `max_hops > 0`.
3. Send at most 30 messages/second (to stay under the per-session rate limiter).
4. Never close the connection.
5. After N seconds, observe that `forward_rate_limiter`'s internal `HashMapStateStore` contains N unique entries and that RSS of the `ckb` process grows proportionally.
6. Assert: after 10^6 messages (~9.3 hours), the HashMap holds ~10^6 entries consuming ~150–200 MB; `retain_recent()` has never been called because `disconnected()` was never triggered.

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
