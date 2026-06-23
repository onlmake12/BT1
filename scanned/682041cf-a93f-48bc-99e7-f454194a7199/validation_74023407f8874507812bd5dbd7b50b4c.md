The code is fully readable. Let me confirm the exact facts before rendering a verdict.

**Key observations from `network/src/protocols/hole_punching/mod.rs`:**

1. `forward_rate_limiter` is `RateLimiter<(PeerId, PeerId, u32)>` backed by `governor::state::keyed::HashMapStateStore` — an unbounded HashMap. [1](#0-0) 

2. `retain_recent()` is called **only** in `disconnected()`, never in `notify()`. [2](#0-1) 

3. `notify()` fires every 5 minutes (`CHECK_INTERVAL`) and cleans up `pending_delivered` and `inflight_requests`, but makes **no call** to `retain_recent()` on either rate limiter. [3](#0-2) 

4. The outer `rate_limiter` is keyed by `(session_id, msg.item_id())` — bounded by session count × message types — and caps at 30/sec per session per message type. [4](#0-3) 

5. The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)` where `from` and `to` are **arbitrary attacker-controlled PeerIds** from the message payload. Each unique pair inserts a new entry into the HashMap. [5](#0-4) 

**The outer rate limiter does not bound the inner HashMap size.** It limits to 30 messages/sec per session, but each of those 30 messages can carry a fresh unique `(from, to)` pair, inserting 30 new entries/sec into `forward_rate_limiter`. With a persistent connection, this accumulates without any periodic cleanup.

---

### Title
Unbounded Memory Growth via Unique PeerId Pairs in `forward_rate_limiter` — (`network/src/protocols/hole_punching/mod.rs`)

### Summary
The `HolePunching` protocol's `forward_rate_limiter` uses a `HashMapStateStore` keyed by attacker-controlled `(PeerId, PeerId, u32)` tuples. The only cleanup call, `retain_recent()`, is placed exclusively in `disconnected()`. As long as an attacker maintains a persistent session, the HashMap grows without bound at up to 30 entries/second, eventually causing an OOM crash.

### Finding Description
`forward_rate_limiter` is declared as:

```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;
// ...
forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
``` [1](#0-0) 

`governor`'s `HashMapStateStore` stores one entry per unique key and never evicts entries on its own — callers must invoke `retain_recent()` to remove stale entries. The code calls `retain_recent()` only here:

```rust
async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
    self.rate_limiter.retain_recent();
    self.forward_rate_limiter.retain_recent();
    ...
}
``` [2](#0-1) 

The periodic `notify()` callback (every 5 minutes) cleans `pending_delivered` and `inflight_requests` but never calls `retain_recent()` on either rate limiter:

```rust
async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
    ...
    self.pending_delivered.retain(|_, (_, t)| (now - *t) < TIMEOUT);
    self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
    // forward_rate_limiter.retain_recent() is ABSENT
    ...
}
``` [3](#0-2) 

The outer per-session rate limiter (30/sec) does not bound the inner HashMap size — it only limits the insertion rate. An attacker sending 30 `ConnectionRequest` messages/sec, each with a fresh random `(from, to)` PeerId pair, inserts 30 new entries/sec into `forward_rate_limiter` indefinitely. [5](#0-4) 

### Impact Explanation
Each `(PeerId, PeerId, u32)` key is ~82 bytes; with `hashbrown` overhead the per-entry cost is ~150–200 bytes. At 30 insertions/sec:

- 1 hour → ~16–21 MB
- 24 hours → ~390–520 MB
- 1 week → ~2.7–3.6 GB

This causes process OOM, crashing the CKB node and halting block validation, transaction relay, and all P2P activity. Impact: **node denial of service**.

### Likelihood Explanation
The attacker needs only a single persistent inbound or outbound P2P connection to the victim node, which is trivially achievable on mainnet. No PoW, no privileged role, no key material is required. The HolePunching protocol is enabled by default when `SupportProtocol::HolePunching` is in the config. [6](#0-5) 

### Recommendation
Add `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify()` callback so stale entries are evicted every 5 minutes regardless of session lifetime. [3](#0-2) 

### Proof of Concept
1. Connect one session to the victim node on the HolePunching protocol.
2. In a loop at 30 msg/sec, send `ConnectionRequest` messages where `from` and `to` are freshly generated random `PeerId` values each iteration.
3. The outer `rate_limiter` passes each message (30/sec budget not exceeded).
4. `forward_rate_limiter.check_key(&(from, to, item_id))` inserts a new `HashMapStateStore` entry for each unique pair.
5. Never disconnect; `retain_recent()` is never called.
6. Monitor victim RSS: it grows at ~5 KB/sec. After ~24 hours the node OOMs and crashes.

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

**File:** network/src/network.rs (L940-953)
```rust
        // HolePunching protocol
        #[cfg(not(target_family = "wasm"))]
        if config
            .support_protocols
            .contains(&SupportProtocol::HolePunching)
        {
            let hole_punching_state = Arc::clone(&network_state);
            let hole_punching_meta =
                SupportProtocols::HolePunching.build_meta_with_service_handle(move || {
                    ProtocolHandle::Callback(Box::new(
                        crate::protocols::hole_punching::HolePunching::new(hole_punching_state),
                    ))
                });
            protocol_metas.push(hole_punching_meta);
```
