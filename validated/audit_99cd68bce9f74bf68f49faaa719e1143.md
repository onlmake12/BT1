### Title
Unbounded `forward_rate_limiter` HashMap Growth via Attacker-Controlled `(from, to)` PeerId Keys — (`network/src/protocols/hole_punching/mod.rs`)

---

### Summary

The `HolePunching` protocol's `forward_rate_limiter` uses a `governor::HashMapStateStore<(PeerId, PeerId, u32)>` keyed on attacker-controlled `from`/`to` fields from incoming messages. `retain_recent()` is called **only** in `disconnected()`, never in `received()` or `notify()`. An attacker holding a single long-lived session can insert an unbounded number of unique keys into the store, causing monotonically growing memory consumption on the victim node.

---

### Finding Description

`forward_rate_limiter` is declared as:

```rust
forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>
```

backed by `governor::state::keyed::HashMapStateStore`. [1](#0-0) 

In `ConnectionSyncProcess::execute()`, the key inserted into the store is:

```rust
self.protocol.forward_rate_limiter
    .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
```

where `content.from` and `content.to` are raw bytes parsed directly from the attacker's message with no validation against known/connected peers. [2](#0-1) 

The same pattern applies to `ConnectionRequest` and `ConnectionRequestDelivered`. [3](#0-2) 

`retain_recent()` is called **only** on disconnect:

```rust
async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
    self.rate_limiter.retain_recent();
    self.forward_rate_limiter.retain_recent();
    ...
}
``` [4](#0-3) 

The `notify()` handler fires every `CHECK_INTERVAL = 5 minutes` and does call `retain` on `pending_delivered` and `inflight_requests`, but **never** on `forward_rate_limiter`:

```rust
async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
    ...
    self.pending_delivered.retain(|_, (_, t)| (now - *t) < TIMEOUT);
    self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
    // forward_rate_limiter.retain_recent() is absent
    ...
}
``` [5](#0-4) 

The outer session-level `rate_limiter` uses key `(session_id, msg.item_id())` and allows 30 messages/sec per session per message type. `msg_item_id` for `ConnectionSync` is the fixed constant `2`. This guard only throttles the insertion rate — it does not bound the total number of unique `(from, to, item_id)` keys. [6](#0-5) 

---

### Impact Explanation

Each unique `(from_i, to_i, 2)` triple inserts a new entry into the `HashMapStateStore`. A PeerId is ~39 bytes; each entry costs roughly 150–200 bytes of heap (key + governor state + HashMap overhead). At 30 inserts/sec per session:

- 1 hour → ~108,000 entries → ~21 MB per session
- With N concurrent attacker sessions → N × 21 MB/hour

The store is never reclaimed until the session disconnects. A long-lived session (or multiple sessions) causes monotonically growing heap usage, leading to performance degradation and eventual OOM on the victim node.

---

### Likelihood Explanation

The attacker needs only one valid P2P connection to the HolePunching protocol. No PoW, no privileged role, no key material is required. The `from` and `to` fields are arbitrary bytes accepted without peer-registry validation. The attack is fully automatable and low-cost.

---

### Recommendation

Call `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify()` handler, which already fires every 5 minutes via `CHECK_INTERVAL`. This mirrors the existing cleanup pattern for `pending_delivered` and `inflight_requests` and bounds the store size to entries active within the last quota window. [7](#0-6) 

---

### Proof of Concept

```rust
// Pseudocode: attacker sends N ConnectionSync messages with unique (from_i, to_i)
// over a single session at rate <= 30/sec to pass the session-level rate_limiter.
for i in 0..N {
    let from = PeerId::random();   // unique each iteration
    let to   = PeerId::random();   // unique each iteration
    let msg  = build_connection_sync(from, to, route=[]);
    session.send(msg);
    sleep(Duration::from_millis(34)); // ~29/sec, under the 30/sec cap
}
// After N iterations, forward_rate_limiter's HashMapStateStore contains N entries.
// retain_recent() has never been called (session still open).
// assert!(forward_rate_limiter_map_size() == N);  // unbounded growth confirmed
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L25-25)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
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
