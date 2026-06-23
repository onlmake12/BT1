### Title
Unbounded `forward_rate_limiter` HashMap Growth via Attacker-Controlled `(from, to)` PeerIds in `ConnectionRequestDelivered` — (`network/src/protocols/hole_punching/mod.rs`)

---

### Summary

An unprivileged remote peer can cause unbounded memory growth in the `HolePunching` protocol handler by sending `ConnectionRequestDelivered` messages with unique attacker-chosen `(from, to)` PeerId pairs over a single long-lived session. The `forward_rate_limiter`'s internal `HashMapStateStore` accumulates one entry per unique key and is only cleaned up via `retain_recent()` on peer disconnect — never periodically — so the HashMap grows without bound for the lifetime of any session.

---

### Finding Description

`HolePunching` holds a `forward_rate_limiter` of type `RateLimiter<(PeerId, PeerId, u32)>`, backed by `governor::state::keyed::HashMapStateStore`: [1](#0-0) 

Every call to `check_key()` on a previously-unseen key inserts a new entry into the HashMap. In `ConnectionRequestDeliveredProcess::execute`, the key is `(content.from, content.to, self.msg_item_id)`: [2](#0-1) 

The `from` and `to` fields are parsed directly from attacker-supplied message bytes with no validation that they correspond to the actual sending peer: [3](#0-2) 

`retain_recent()` — which evicts expired entries from the HashMap — is called **only** in `disconnected()`: [4](#0-3) 

The periodic `notify()` handler (fires every 5 minutes) cleans up `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on either rate limiter: [5](#0-4) 

---

### Impact Explanation

An attacker maintains a single long-lived TCP session. The outer per-session rate limiter (`rate_limiter` keyed by `(session_id, item_id)`) allows 30 `ConnectionRequestDelivered` messages per second. Each message with a fresh `(from, to)` pair inserts a new entry into `forward_rate_limiter`. At 30 entries/second over 24 hours, this yields ~2.6 million entries. Each entry holds two `PeerId` values (~39 bytes each) plus governor state, so memory growth is on the order of hundreds of MB per day per attacker session, with no eviction until the attacker disconnects. Multiple concurrent attacker sessions multiply the impact linearly.

---

### Likelihood Explanation

The attack requires only a standard P2P connection to the HolePunching protocol — no authentication, no PoW, no privileged role. The attacker simply sends well-formed `ConnectionRequestDelivered` messages with unique `from`/`to` byte sequences. The outer rate limiter throttles but does not prevent the attack; it merely sets the growth rate.

---

### Recommendation

Call `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside `notify()`, which already fires every 5 minutes via `CHECK_INTERVAL`: [6](#0-5) 

This mirrors the cleanup already done in `disconnected()` and bounds the HashMap to entries active within the last rate-limit window, regardless of session lifetime.

---

### Proof of Concept

```rust
// Pseudocode unit test
let mut hp = HolePunching::new(network_state);
for i in 0..10_000u32 {
    let from = PeerId::random();
    let to   = PeerId::random();
    let msg  = build_connection_request_delivered(from, to, /*route=*/vec![], ...);
    hp.received(mock_ctx(), msg.as_bytes()).await;
    // No disconnect between iterations
}
// Assert: forward_rate_limiter internal map has ~10_000 entries, not 0
// retain_recent() was never called; all entries survive
```

Each unique `(from, to)` pair passes the rate limiter check (1/second quota not yet exhausted for that key) and inserts a new HashMap entry. Without a disconnect, `retain_recent()` is never invoked, so all entries accumulate.

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

**File:** network/src/protocols/hole_punching/mod.rs (L169-175)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let status = self.network_state.connection_status();

        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L37-42)
```rust
    fn try_from(value: &packed::ConnectionRequestDeliveredReader<'_>) -> Result<Self, Self::Error> {
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
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
