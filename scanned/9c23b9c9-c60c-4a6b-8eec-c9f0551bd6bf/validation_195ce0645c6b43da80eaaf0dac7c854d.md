### Title
Unbounded `forward_rate_limiter` HashMap Growth via Unique `(from, to)` PeerId Pairs in ConnectionSync — (`network/src/protocols/hole_punching/mod.rs`)

### Summary

The `HolePunching` protocol's `forward_rate_limiter` uses a `HashMapStateStore` keyed by `(PeerId, PeerId, u32)`. Because `retain_recent()` is only called in `disconnected()` and never in `received()` or `notify()`, an attacker maintaining a long-lived session can insert new entries into the HashMap indefinitely by sending ConnectionSync messages with unique attacker-controlled `(from, to)` PeerId pairs, causing unbounded memory growth proportional to session duration.

---

### Finding Description

**Type:** `RateLimiter<(PeerId, PeerId, u32)>` — `governor::state::keyed::HashMapStateStore`

The `forward_rate_limiter` is declared as:

```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;
// ...
forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
``` [1](#0-0) 

`retain_recent()` is called **only** in `disconnected()`:

```rust
async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
    self.rate_limiter.retain_recent();
    self.forward_rate_limiter.retain_recent();
    ...
}
``` [2](#0-1) 

It is **never** called in `received()` or `notify()`. The `notify()` handler (firing every 5 minutes) cleans up `pending_delivered` and `inflight_requests` but ignores both rate limiters: [3](#0-2) 

In `ConnectionSyncProcess::execute()`, the rate-limiter key is built from attacker-supplied message fields:

```rust
self.protocol
    .forward_rate_limiter
    .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
``` [4](#0-3) 

`content.from` and `content.to` are parsed directly from the wire message with no validation that they match the actual session's peer identity: [5](#0-4) 

`msg_item_id` for `ConnectionSync` is always the fixed constant `2`: [6](#0-5) 

The outer per-session `rate_limiter` (keyed by `(PeerIndex, u32)`) caps the attacker at 30 msgs/sec per session per message type, but this only bounds the **insertion rate** — it does not bound the **total HashMap size** over time. [7](#0-6) 

---

### Impact Explanation

Each unique `(from_i, to_i, 2)` triple inserts a new entry into the `HashMapStateStore`. With 30 msgs/sec and unique pairs per message:

- After 1 hour: ~108,000 entries
- Per-entry cost: ~39 (PeerId) + ~39 (PeerId) + 4 (u32) + governor `AtomicU64` state + HashMap bucket overhead ≈ ~150–200 bytes
- Single session, 1 hour: ~16–21 MB
- Single session, 24 hours: ~400–500 MB
- Multiple concurrent long-lived sessions scale this linearly

The `notify()` interval is 5 minutes (`CHECK_INTERVAL = Duration::from_secs(5 * 60)`), which is a natural place to call `retain_recent()` but does not do so: [8](#0-7) 

Memory is only reclaimed when the session disconnects. A single attacker maintaining a persistent connection can cause slow but unbounded memory growth, degrading node stability over hours to days.

---

### Likelihood Explanation

- Requires only a standard P2P connection — no privilege, no PoW, no key material.
- The attacker controls `from` and `to` fields freely; no cryptographic binding to the session identity is enforced.
- The outer 30 req/sec rate limit is a real cap but does not prevent the accumulation — it only slows it.
- The attack is passive and low-bandwidth (~30 small messages/sec), making it difficult to detect via traffic analysis.

---

### Recommendation

Call `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside `notify()` in addition to `disconnected()`. Since `notify()` already fires every 5 minutes, this would bound the maximum HashMap size to at most `30 req/sec × 300 sec = 9,000 entries` regardless of session duration — a constant upper bound. [3](#0-2) 

---

### Proof of Concept

Call sequence:
1. Attacker establishes a single long-lived P2P session to the victim node.
2. Attacker sends 30 `ConnectionSync` messages/sec, each with a freshly generated unique `(from_i, to_i)` PeerId pair (valid multihash bytes, but otherwise arbitrary).
3. Each message passes the outer `rate_limiter` check (30/sec per `(PeerIndex, 2)` is the cap) and reaches `forward_rate_limiter.check_key(...)`.
4. Each unique key inserts a new entry into `HashMapStateStore`; `retain_recent()` is never called during the session.
5. After T seconds, the HashMap contains `30 × T` entries.
6. At T = 86,400 s (24 hours): ~2.6 million entries consuming ~400–500 MB per session.

Unit test assertion: after simulating N messages with unique keys, `forward_rate_limiter` internal map size equals N (not bounded by a constant), confirming the invariant is broken.

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L25-26)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L42-47)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
        Ok(SyncContent { route, from, to })
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

**File:** util/gen-types/src/generated/protocols.rs (L5579-5584)
```rust
    pub fn item_id(&self) -> molecule::Number {
        match self {
            HolePunchingMessageUnionReader::ConnectionRequest(_) => 0,
            HolePunchingMessageUnionReader::ConnectionRequestDelivered(_) => 1,
            HolePunchingMessageUnionReader::ConnectionSync(_) => 2,
        }
```
